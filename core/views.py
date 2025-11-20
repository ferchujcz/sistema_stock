# core/views.py

from fuzzywuzzy import fuzz
import io
from django.http import HttpResponse
import json

# --- Imports de Django ---
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.db import transaction, IntegrityError
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Count,Case, When, IntegerField, Min, F, Q
from django.contrib.auth.decorators import login_required
from apyori import apriori

# --- Imports de Python ---
import json
import os
import re
from datetime import timedelta,datetime
from decimal import Decimal, ROUND_HALF_UP

# --- Imports de Terceros ---
import pandas as pd
try:
    from google.cloud import vision
except ImportError:
    vision = None # Permite que el servidor corra si no está instalada la librería

# --- Import de Modelos Locales ---
from .models import (
    Producto, Stock, Venta, DetalleVenta, Configuracion, Proveedor,
    Categoria, Sucursal, PerfilUsuario,Cliente, PagoCliente, EnvaseRetornable, StockEnvases, FacturaProveedor, PagoProveedor, CierreTurno, PrediccionVenta
)

# --- Helper Function ---
def obtener_sucursal_usuario(request):
    """Obtiene la sucursal del usuario logueado o None si no la tiene."""
    try:
        perfil = getattr(request.user, 'perfilusuario', None)
        if perfil and perfil.sucursal:
            return perfil.sucursal
    except PerfilUsuario.DoesNotExist:
        pass
    return None

# ==============================================================================
# VISTA PRINCIPAL (DASHBOARD)
# ==============================================================================
@login_required
def dashboard(request):
    context = {}
    usuario = request.user
    hoy = timezone.now().date()
    sucursal_usuario = obtener_sucursal_usuario(request)

    # --- 1. CÁLCULO DE GRÁFICO (Ventas últimos 7 días) ---
    # Esto faltaba en la versión anterior y por eso daba error el JS
    ventas_semana_labels = []
    ventas_semana_data = []
    
    # Iteramos los últimos 7 días (incluyendo hoy)
    for i in range(6, -1, -1):
        dia = hoy - timedelta(days=i)
        # Filtramos ventas por día
        ventas_dia_query = Venta.objects.filter(fecha_hora__date=dia)
        
        # Si hay sucursal, filtramos. Si es superadmin global, ve todo.
        if sucursal_usuario:
            ventas_dia_query = ventas_dia_query.filter(sucursal=sucursal_usuario)
        
        total_dia = ventas_dia_query.aggregate(total=Sum('total'))['total'] or 0
        
        ventas_semana_labels.append(dia.strftime('%d/%m')) # Etiqueta eje X
        ventas_semana_data.append(float(total_dia)) # Dato eje Y

    # Guardamos los datos del gráfico en el contexto
    context['ventas_labels'] = json.dumps(ventas_semana_labels)
    context['ventas_data'] = json.dumps(ventas_semana_data)
    context['total_ventas_semana'] = sum(ventas_semana_data) # Esta variable faltaba
    # -----------------------------------------------------

    # --- 2. LÓGICA DE SUPERADMIN ---
    if usuario.is_superuser:
        ventas_hoy_todas = Venta.objects.filter(fecha_hora__date=hoy)
        total_vendido_global = ventas_hoy_todas.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
        ventas_por_sucursal = ventas_hoy_todas.values('sucursal__nombre').annotate(total_vendido=Sum('total')).order_by('sucursal__nombre')

        context['ventas_por_sucursal'] = ventas_por_sucursal
        context['total_vendido_global'] = total_vendido_global
        context['es_superadmin'] = True

    # --- 3. LÓGICA DE SUCURSAL (Widgets y Predicciones) ---
    # Mostramos datos si el usuario tiene sucursal (o es admin con sucursal asignada en perfil)
    if sucursal_usuario:
        context['sucursal_actual'] = sucursal_usuario

        # A. Ventas del día
        ventas_hoy_sucursal = Venta.objects.filter(fecha_hora__date=hoy, sucursal=sucursal_usuario)
        total_vendido_hoy_sucursal = ventas_hoy_sucursal.aggregate(total=Sum('total'))['total'] or Decimal('0.00')
        numero_ventas_hoy_sucursal = ventas_hoy_sucursal.count()
        context['total_vendido_hoy'] = total_vendido_hoy_sucursal
        context['numero_ventas_hoy'] = numero_ventas_hoy_sucursal

        # B. Predicciones (IA) - CORREGIDO
        # Buscamos predicciones desde HOY en adelante para esta sucursal
        predicciones = PrediccionVenta.objects.filter(
            sucursal=sucursal_usuario,
            fecha__gte=hoy
        ).aggregate(total_predicho=Sum('cantidad_predicha'))
        
        # Si no hay predicción, devolvemos 0
        context['prediccion_7_dias'] = predicciones['total_predicho'] or 0

        # C. Alertas de vencimiento
        alertas_vencimiento = Stock.objects.filter(
            sucursal=sucursal_usuario,
            fecha_vencimiento__lte=hoy + timedelta(days=20),
            fecha_vencimiento__gte=hoy,
            cantidad__gt=0
        ).order_by('fecha_vencimiento')[:5]
        context['alertas_vencimiento'] = alertas_vencimiento

        # D. Alertas de Stock Bajo
        productos_con_stock_sucursal = Producto.objects.annotate(
            stock_total_sucursal=Sum('lotes__cantidad', filter=Q(lotes__sucursal=sucursal_usuario))
        ).filter(stock_total_sucursal__isnull=False)
        alertas_stock_bajo = productos_con_stock_sucursal.filter(
            stock_total_sucursal__lt=F('stock_minimo')
        ).order_by('stock_total_sucursal')[:5]
        context['alertas_stock_bajo'] = alertas_stock_bajo
        
        # E. Alertas de Stock Sin Fecha (Perecederos)
        alertas_sin_fecha = Producto.objects.filter(
            es_perecedero=True,
            lotes__sucursal=sucursal_usuario,
            lotes__cantidad__gt=0,
            lotes__fecha_vencimiento__isnull=True
        ).distinct()
        context['alertas_sin_fecha'] = alertas_sin_fecha

    elif not usuario.is_superuser:
        messages.warning(request, "Tu usuario no está asignado a ninguna sucursal. Contacta al administrador.")

    return render(request, 'core/dashboard.html', context)

# ==============================================================================
# VISTAS DE STOCK (Filtradas por Sucursal)
# ==============================================================================

@login_required
def admin_stock_por_sucursal(request, sucursal_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('dashboard')
    # --- FIN PERMISO ---

    sucursal = get_object_or_404(Sucursal, id=sucursal_id)
    hoy = timezone.now().date()
    hace_30_dias = hoy - timedelta(days=30)

    # Usamos la sucursal de la URL para filtrar
    productos_con_stock = Producto.objects.filter(
        lotes__cantidad__gt=0,
        lotes__sucursal=sucursal # <-- Filtro por sucursal seleccionada
    ).annotate(
        total_gondola=Sum(Case(When(lotes__ubicacion='gondola', lotes__sucursal=sucursal, then='lotes__cantidad'), default=0, output_field=IntegerField())),
        total_deposito=Sum(Case(When(lotes__ubicacion='deposito', lotes__sucursal=sucursal, then='lotes__cantidad'), default=0, output_field=IntegerField())),
        vencimiento_proximo=Min('lotes__fecha_vencimiento', filter=Q(lotes__sucursal=sucursal))
    ).distinct()

    info_consolidada = []
    for producto in productos_con_stock:
        dias_para_vencer = None
        if producto.vencimiento_proximo:
            dias_para_vencer = (producto.vencimiento_proximo - hoy).days

        ventas_30_dias = DetalleVenta.objects.filter(
            producto=producto, venta__fecha_hora__gte=hace_30_dias
        ).aggregate(total_vendido=Sum('cantidad'))['total_vendido'] or 0
        velocidad_venta = ventas_30_dias / 30.0 if ventas_30_dias > 0 else 0 # Evitar división por cero

        en_riesgo = False
        stock_total = (producto.total_gondola or 0) + (producto.total_deposito or 0)
        if velocidad_venta > 0 and dias_para_vencer is not None and dias_para_vencer > 0:
            dias_de_stock_restante = stock_total / velocidad_venta
            if dias_de_stock_restante > dias_para_vencer:
                en_riesgo = True

        info_consolidada.append({
            'producto': producto, 'total_gondola': producto.total_gondola,
            'total_deposito': producto.total_deposito,
            'vencimiento_proximo': producto.vencimiento_proximo,
            'dias_para_vencer': dias_para_vencer,
            'dias_para_vencer_abs': abs(dias_para_vencer) if dias_para_vencer is not None else None,
            'en_riesgo': en_riesgo, 'velocidad_venta': round(velocidad_venta, 2)
        })
    info_consolidada.sort(key=lambda x: (x['dias_para_vencer'] is None, x['dias_para_vencer'] if x['dias_para_vencer'] is not None else float('inf')))
    # Usaremos la misma plantilla que stock_detalle, pero pasándole la sucursal que estamos viendo
    return render(request, 'core/stock_detalle.html', {
        'info_consolidada': info_consolidada,
        'sucursal_seleccionada': sucursal # Para mostrar el nombre en la plantilla
    })




@login_required
def stock_detalle(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario and not request.user.is_superuser:
        messages.error(request, "Tu usuario no está asignado a ninguna sucursal.")
        return render(request, 'core/stock_detalle.html', {'info_consolidada': []})

    hoy = timezone.now().date()
    hace_30_dias = hoy - timedelta(days=30)

    base_query = Producto.objects.all()
    if not request.user.is_superuser and sucursal_usuario:
        base_query = Producto.objects.filter(lotes__sucursal=sucursal_usuario)

    productos_con_stock = base_query.filter(lotes__cantidad__gt=0).annotate(
        total_gondola=Sum(Case(When(lotes__ubicacion='gondola', lotes__sucursal=sucursal_usuario, then='lotes__cantidad'), default=0, output_field=IntegerField())) if sucursal_usuario else Sum(Case(When(lotes__ubicacion='gondola', then='lotes__cantidad'), default=0, output_field=IntegerField())),
        total_deposito=Sum(Case(When(lotes__ubicacion='deposito', lotes__sucursal=sucursal_usuario, then='lotes__cantidad'), default=0, output_field=IntegerField())) if sucursal_usuario else Sum(Case(When(lotes__ubicacion='deposito', then='lotes__cantidad'), default=0, output_field=IntegerField())),
        vencimiento_proximo=Min('lotes__fecha_vencimiento', filter=Q(lotes__sucursal=sucursal_usuario)) if sucursal_usuario else Min('lotes__fecha_vencimiento')
    ).distinct()

    info_consolidada = []
    for producto in productos_con_stock:
        dias_para_vencer = None
        if producto.vencimiento_proximo:
            dias_para_vencer = (producto.vencimiento_proximo - hoy).days

        ventas_30_dias = DetalleVenta.objects.filter(
            producto=producto, venta__fecha_hora__gte=hace_30_dias
        ).aggregate(total_vendido=Sum('cantidad'))['total_vendido'] or 0
        velocidad_venta = ventas_30_dias / 30.0 if ventas_30_dias > 0 else 0 # Evitar división por cero

        en_riesgo = False
        stock_total = (producto.total_gondola or 0) + (producto.total_deposito or 0)
        if velocidad_venta > 0 and dias_para_vencer is not None and dias_para_vencer > 0:
            dias_de_stock_restante = stock_total / velocidad_venta
            if dias_de_stock_restante > dias_para_vencer:
                en_riesgo = True

        info_consolidada.append({
            'producto': producto, 'total_gondola': producto.total_gondola,
            'total_deposito': producto.total_deposito,
            'vencimiento_proximo': producto.vencimiento_proximo,
            'dias_para_vencer': dias_para_vencer,
            'dias_para_vencer_abs': abs(dias_para_vencer) if dias_para_vencer is not None else None,
            'en_riesgo': en_riesgo, 'velocidad_venta': round(velocidad_venta, 2)
        })

    info_consolidada.sort(key=lambda x: (x['dias_para_vencer'] is None, x['dias_para_vencer'] if x['dias_para_vencer'] is not None else float('inf'))) # Ordenar nulos al final
    return render(request, 'core/stock_detalle.html', {'info_consolidada': info_consolidada, 'sucursal_actual': sucursal_usuario})

@login_required
def agregar_stock(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
         messages.error(request, "Tu usuario no está asignado a una sucursal para añadir stock.")
         return redirect('dashboard')

    if request.method == 'POST':
        producto_id = request.POST['producto']
        cantidad = int(request.POST['cantidad'])
        fecha_vencimiento = request.POST.get('fecha_vencimiento')
        ubicacion = request.POST['ubicacion']
        producto = get_object_or_404(Producto, id=producto_id)

        Stock.objects.create(
            producto=producto, cantidad=cantidad,
            fecha_vencimiento=fecha_vencimiento if fecha_vencimiento else None,
            ubicacion=ubicacion,
            sucursal=sucursal_usuario
        )
        messages.success(request, f"Stock añadido para {producto.nombre}. ¡Listo para el siguiente!")
        return redirect('agregar_stock')

    
    return render(request, 'core/agregar_stock.html')

@login_required
def editar_stock(request, stock_id):
    sucursal_usuario = obtener_sucursal_usuario(request)
    stock_item = get_object_or_404(Stock, id=stock_id)

    if not request.user.is_superuser and stock_item.sucursal != sucursal_usuario:
        messages.error(request, "No tienes permiso para editar stock de otra sucursal.")
        return redirect('stock_detalle')

    if request.method == 'POST':
        stock_item.cantidad = int(request.POST['cantidad'])
        fecha_vencimiento = request.POST.get('fecha_vencimiento')
        stock_item.fecha_vencimiento = fecha_vencimiento if fecha_vencimiento else None
        stock_item.ubicacion = request.POST['ubicacion']
        stock_item.save()
        messages.success(request, f"Lote de {stock_item.producto.nombre} actualizado.")
        return redirect('detalle_producto_lotes', producto_id=stock_item.producto.id)

    return render(request, 'core/editar_stock.html', {'stock_item': stock_item})

@login_required
def reponer_gondola(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
         messages.error(request, "Usuario sin sucursal asignada.")
         return redirect('dashboard')

    if request.method == 'POST':
        with transaction.atomic():
            items_movidos = 0
            for key, value in request.POST.items():
                if key.startswith('cantidad_a_mover_') and value:
                    stock_id = key.split('_')[-1]
                    try:
                        cantidad_a_mover = int(value)
                        if cantidad_a_mover > 0:
                            lote_deposito = get_object_or_404(Stock, id=stock_id, sucursal=sucursal_usuario, ubicacion='deposito')
                            if lote_deposito.cantidad >= cantidad_a_mover:
                                lote_deposito.cantidad -= cantidad_a_mover
                                lote_deposito.save()

                                lote_gondola, created = Stock.objects.get_or_create(
                                    producto=lote_deposito.producto,
                                    fecha_vencimiento=lote_deposito.fecha_vencimiento,
                                    ubicacion='gondola',
                                    sucursal=sucursal_usuario,
                                    defaults={'cantidad': 0}
                                )
                                lote_gondola.cantidad += cantidad_a_mover
                                lote_gondola.save()
                                items_movidos += 1
                            else:
                                messages.warning(request, f"No hay suficiente stock en depósito para {lote_deposito.producto.nombre} (Lote: {stock_id}).")
                    except (ValueError, Stock.DoesNotExist):
                        messages.error(request, f"Error al procesar el lote con ID {stock_id}.")
                        # Forzamos que la transacción falle para deshacer cambios
                        raise ValueError("Error procesando lote")

            if items_movidos > 0: messages.success(request, f"Se movieron {items_movidos} items a la góndola.")
            else: messages.info(request, "No se especificaron cantidades válidas para mover.")
        return redirect('stock_detalle') # Siempre redirigir, incluso si hubo warning

    stock_en_deposito = Stock.objects.filter(sucursal=sucursal_usuario, ubicacion='deposito', cantidad__gt=0).order_by('producto__nombre', 'fecha_vencimiento')
    return render(request, 'core/reponer_gondola.html', {'stock_en_deposito': stock_en_deposito})

@login_required
def detalle_producto_lotes(request, producto_id):
    sucursal_usuario = obtener_sucursal_usuario(request)
    producto = get_object_or_404(Producto, id=producto_id)

    lotes_query = Stock.objects.filter(producto=producto, cantidad__gt=0)
    if not request.user.is_superuser and sucursal_usuario:
        lotes_query = lotes_query.filter(sucursal=sucursal_usuario)
    elif not request.user.is_superuser: # Si no es superuser y no tiene sucursal
        lotes_query = Stock.objects.none()

    lotes = lotes_query.order_by('fecha_vencimiento')
    return render(request, 'core/detalle_producto_lotes.html', {'producto': producto, 'lotes': lotes, 'sucursal_actual': sucursal_usuario})



@login_required
def contar_inventario(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Necesitas una sucursal asignada para realizar un inventario.")
        return redirect('dashboard')

    if request.method == 'POST':
        # --- Procesamiento del conteo ---
        items_contados = {}
        # Recolectamos los datos del formulario (product_id -> cantidad)
        for key, value in request.POST.items():
            if key.startswith('cantidad_contada_'):
                try:
                    producto_id = int(key.split('_')[-1])
                    cantidad = int(value)
                    if cantidad >= 0: # Aceptamos 0 si no se encontró nada
                        items_contados[producto_id] = cantidad
                except (ValueError, TypeError):
                    messages.warning(request, f"Se recibió un dato inválido para el producto ID {key.split('_')[-1]}.")

        # Obtenemos el stock actual del sistema PARA ESTA SUCURSAL
        stock_sistema_raw = Stock.objects.filter(sucursal=sucursal_usuario, cantidad__gt=0)
        stock_sistema = {}
        for item in stock_sistema_raw:
            stock_sistema[item.producto_id] = stock_sistema.get(item.producto_id, 0) + item.cantidad

        # Comparamos y calculamos discrepancias
        discrepancias = []
        todos_los_productos_ids = set(items_contados.keys()) | set(stock_sistema.keys())

        for prod_id in todos_los_productos_ids:
            contado = items_contados.get(prod_id, 0)
            sistema = stock_sistema.get(prod_id, 0)
            diferencia = contado - sistema

            if diferencia != 0: # Solo mostramos si hay diferencia
                producto = Producto.objects.get(id=prod_id) # Obtenemos el objeto producto para el nombre
                discrepancias.append({
                    'producto_id': producto.id,
                    'producto_nombre': producto.nombre,
                    'contado': contado,
                    'sistema': sistema,
                    'diferencia': diferencia, # Positivo = sobrante, Negativo = faltante
                })

        # Ordenamos por nombre de producto
        discrepancias.sort(key=lambda x: x['producto_nombre'])

        return render(request, 'core/resultado_inventario.html', {
            'discrepancias': discrepancias,
            'sucursal_actual': sucursal_usuario
        })

    # --- Si es GET, mostramos la página de conteo ---
    # Pasamos todos los productos para la búsqueda inicial (opcionalmente podríamos filtrar por los que tienen stock)
    productos = Producto.objects.all().order_by('nombre')
    return render(request, 'core/contar_inventario.html', {'productos': productos})

@login_required
def aplicar_ajuste_inventario(request):
    if request.method != 'POST':
        return redirect('dashboard')

    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "No tienes sucursal asignada.")
        return redirect('dashboard')

    ajustes_realizados = 0

    try:
        with transaction.atomic(): # Si algo falla, no se guarda nada
            for key, value in request.POST.items():
                # Buscamos los inputs que se llamen 'ajuste_PRODUCTOID'
                if key.startswith('ajuste_'):
                    producto_id = int(key.split('_')[1])
                    diferencia = int(value)
                    
                    if diferencia == 0: continue # Si no hay diferencia, saltamos

                    producto = Producto.objects.get(id=producto_id)

                    # CASO A: FALTANTE (La diferencia es negativa, ej: -5)
                    if diferencia < 0:
                        cantidad_a_restar = abs(diferencia)
                        
                        # Lógica FEFO: Buscamos lotes con stock, ordenados por vencimiento (los que vencen antes primero)
                        lotes = Stock.objects.filter(
                            producto=producto,
                            sucursal=sucursal_usuario,
                            cantidad__gt=0
                        ).order_by(F('fecha_vencimiento').asc(nulls_last=True)) 

                        for lote in lotes:
                            if cantidad_a_restar <= 0: break
                            
                            descuento = min(lote.cantidad, cantidad_a_restar)
                            lote.cantidad -= descuento
                            lote.save()
                            
                            cantidad_a_restar -= descuento
                        
                        # Si después de recorrer todos los lotes todavía falta restar, es que el sistema
                        # pensaba que tenía más de lo que realmente había en lotes.
                        # (Podríamos crear un registro de pérdida aquí si tuviéramos ese modelo)

                    # CASO B: SOBRANTE (La diferencia es positiva, ej: +5)
                    else:
                        # Buscamos o creamos un lote "Sin Vencimiento" en Depósito
                        lote_sobrante, created = Stock.objects.get_or_create(
                            producto=producto,
                            sucursal=sucursal_usuario,
                            fecha_vencimiento=None, # Sin fecha
                            ubicacion='deposito',   # Por defecto a depósito
                            defaults={'cantidad': 0}
                        )
                        lote_sobrante.cantidad += diferencia
                        lote_sobrante.save()

                    ajustes_realizados += 1

            if ajustes_realizados > 0:
                messages.success(request, f"¡Stock actualizado! Se ajustaron {ajustes_realizados} productos.")
            else:
                messages.info(request, "No hubo diferencias para ajustar.")

    except Exception as e:
        messages.error(request, f"Error al aplicar ajustes: {e}")
    
    return redirect('stock_detalle')

# ==============================================================================
# VISTAS DE VENTAS (Filtradas/Asignadas por Sucursal)
# ==============================================================================
@login_required
def registrar_venta(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "No puedes registrar ventas sin una sucursal asignada.")
        return redirect('dashboard')

    config, created = Configuracion.objects.get_or_create(pk=1)

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            carrito = data.get('carrito', [])
            metodo_pago = data.get('metodo_pago', 'efectivo')
            cuotas = int(data.get('cuotas', 1))
            cliente_id = data.get('cliente_id') # <-- ¡CORRECCIÓN 1: CAPTURAR CLIENTE ID!
            cliente = None

            if not carrito: 
                return JsonResponse({'error': 'El carrito está vacío'}, status=400)

            with transaction.atomic():
                # --- Lógica de Cliente y Límite ---
                if metodo_pago == 'cuenta_corriente':
                    if not cliente_id:
                        raise Exception('Para "Cuenta Corriente", debes seleccionar un cliente.')
                    cliente = get_object_or_404(Cliente, id=cliente_id)
                
                # Calculamos el subtotal de PRODUCTOS (ignorando devoluciones)
                subtotal_productos = sum(Decimal(str(item['precio'])) * int(item['cantidad']) for item in carrito if item.get('tipo') != 'devolucion')
                # Calculamos el total de DEVOLUCIONES
                total_devoluciones = sum(Decimal(str(item['precio'])) * int(item['cantidad']) for item in carrito if item.get('tipo') == 'devolucion')

                subtotal_venta = subtotal_productos + total_devoluciones # El subtotal real

                descuento_recargo = Decimal('0.00')
                # Los recargos/descuentos se aplican solo sobre el subtotal de productos
                if metodo_pago == 'efectivo' and config.descuento_efectivo_porcentaje > 0: 
                    descuento_recargo = -(subtotal_productos * (config.descuento_efectivo_porcentaje / Decimal('100')))
                elif metodo_pago == 'credito' and config.recargo_credito_porcentaje > 0: 
                    descuento_recargo = subtotal_productos * (config.recargo_credito_porcentaje / Decimal('100'))
                elif metodo_pago == 'qr' and config.recargo_qr_porcentaje > 0: 
                    descuento_recargo = subtotal_productos * (config.recargo_qr_porcentaje / Decimal('100'))

                total_venta = subtotal_venta + descuento_recargo
                total_venta_quantized = total_venta.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                # Chequeo de límite de crédito
                if cliente and (cliente.saldo_actual + total_venta_quantized) > cliente.limite_credito:
                    raise Exception(f'Límite de crédito excedido. Saldo actual: ${cliente.saldo_actual}. Límite: ${cliente.limite_credito}.')

                nueva_venta = Venta.objects.create(
                    subtotal=subtotal_venta, 
                    descuento_recargo=descuento_recargo.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    total=total_venta_quantized, 
                    metodo_pago=metodo_pago, 
                    cuotas=cuotas,
                    sucursal=sucursal_usuario,
                    cliente=cliente # Asigna el cliente (o None)
                )

                # Si fue fiado, actualizamos el saldo del cliente
                if cliente:
                    cliente.saldo_actual += total_venta_quantized
                    cliente.save()

                for item in carrito:
                    if item.get('tipo') == 'devolucion':
                        # --- ES UNA DEVOLUCIÓN DE ENVASE ---
                        envase_id_num = int(item['id'].split('_')[1])
                        cantidad_devuelta = int(item['cantidad'])
                        envase = get_object_or_404(EnvaseRetornable, id=envase_id_num)
                        
                        stock_envase, created = StockEnvases.objects.get_or_create(
                            envase=envase,
                            sucursal=sucursal_usuario,
                            defaults={'cantidad_vacia': 0}
                        )
                        stock_envase.cantidad_vacia += cantidad_devuelta
                        stock_envase.save()

                        # Registramos en DetalleVenta
                        DetalleVenta.objects.create(
                            venta=nueva_venta,
                            producto=None, # Es un ajuste, no un producto
                            cantidad=cantidad_devuelta,
                            precio_unitario=item['precio'], # Precio negativo
                            subtotal=Decimal(str(item['precio'])) * cantidad_devuelta
                        )

                    else:
                        # --- ES UN PRODUCTO NORMAL ---
                        producto = get_object_or_404(Producto, id=item['id'])
                        cantidad_a_vender = int(item['cantidad'])
                        
                        lotes_disponibles = Stock.objects.filter(producto=producto, cantidad__gt=0, ubicacion='gondola', sucursal=sucursal_usuario).order_by('fecha_vencimiento')
                        cantidad_vendida_total = 0
                        for lote in lotes_disponibles:
                            if cantidad_vendida_total >= cantidad_a_vender: break
                            cantidad_a_descontar = min(lote.cantidad, cantidad_a_vender - cantidad_vendida_total)
                            lote.cantidad -= cantidad_a_descontar
                            lote.save()
                            cantidad_vendida_total += cantidad_a_descontar

                        if cantidad_vendida_total < cantidad_a_vender:
                            raise Exception(f"Stock insuficiente en esta sucursal para {producto.nombre} (necesitas {cantidad_a_vender}, disponibles {cantidad_vendida_total})")

                        subtotal_detalle = Decimal(str(item['precio'])) * int(item['cantidad'])
                        DetalleVenta.objects.create(
                            venta=nueva_venta, producto=producto, cantidad=item['cantidad'],
                            precio_unitario=producto.precio_venta, subtotal=subtotal_detalle
                        )

                return JsonResponse({'success': True, 'venta_id': nueva_venta.id, 'mensaje': f"Venta registrada! Total: ${total_venta_quantized}"})
        
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    # --- LÓGICA GET (CORREGIDA) ---
    productos_favoritos = Producto.objects.filter(
        es_favorito=True,
        lotes__cantidad__gt=0,
        lotes__sucursal=sucursal_usuario
    ).distinct().order_by('nombre')
    
    envases_retornables = EnvaseRetornable.objects.all().order_by('nombre')
    
    context = {
        'config': config,
        'productos_favoritos': productos_favoritos,
        'envases_retornables': envases_retornables # <-- ¡CORRECCIÓN 2: AÑADIR ENVASES AL CONTEXTO!
    }
    return render(request, 'core/registrar_venta.html', context)

@login_required
def historial_ventas(request):
    sucursal_usuario = obtener_sucursal_usuario(request)

    ventas_query = Venta.objects.all()
    if not request.user.is_superuser and sucursal_usuario:
        ventas_query = ventas_query.filter(sucursal=sucursal_usuario)
    elif not request.user.is_superuser:
        ventas_query = Venta.objects.none()
        messages.warning(request, "No tienes una sucursal asignada para ver el historial.")

    ventas = ventas_query.order_by('-fecha_hora').prefetch_related('detalles__producto')
    return render(request, 'core/historial_ventas.html', {'ventas': ventas, 'sucursal_actual': sucursal_usuario})

@login_required
def detalle_venta(request, venta_id):
    sucursal_usuario = obtener_sucursal_usuario(request)
    # Obtenemos la venta y cargamos sus detalles y productos relacionados
    venta = get_object_or_404(Venta.objects.prefetch_related('detalles__producto'), id=venta_id)

    # Seguridad: El superadmin puede ver todo, el empleado solo las de su sucursal
    if not request.user.is_superuser and sucursal_usuario != venta.sucursal:
        messages.error(request, "No tienes permiso para ver esta venta.")
        return redirect('historial_ventas')

    return render(request, 'core/detalle_venta.html', {'venta': venta})
# ==============================================================================
# VISTAS API (Búsquedas)
# ==============================================================================
@login_required
def buscar_productos(request):
    query = request.GET.get('term', '')
    # Solo mostramos productos que tengan stock EN ALGUNA PARTE (opcional, pero puede mejorar perf)
    productos = Producto.objects.filter(nombre__icontains=query)[:10]
    resultados = [{'id': p.id, 'nombre': p.nombre, 'precio': p.precio_venta} for p in productos]
    return JsonResponse(resultados, safe=False)

@login_required
def buscar_producto_por_codigo(request):
    codigo = request.GET.get('codigo', '')
    try:
        # Buscamos el producto, no importa el stock aquí
        producto = Producto.objects.get(codigo_barras=codigo)
        resultado = {'id': producto.id, 'nombre': producto.nombre, 'precio': producto.precio_venta}
        return JsonResponse(resultado)
    except Producto.DoesNotExist:
         return JsonResponse({'error': 'Producto no encontrado'}, status=404)

# ==============================================================================
# VISTAS DE IMPORTACIÓN E IA
# ==============================================================================
@login_required
def importar_stock_excel(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
         messages.error(request, "Necesitas una sucursal asignada para importar stock.")
         return redirect('dashboard')

    if request.method == 'POST':
        archivo = request.FILES.get('archivo_excel')
        if not archivo:
            messages.error(request, "No se seleccionó ningún archivo.")
            return redirect('importar_stock')

        try:
            df = pd.read_excel(archivo, dtype={'codigo_barras': str})

            # 1. Obtenemos los datos actuales de la BD para comparar
            # Usamos diccionarios para una búsqueda rápida
            proveedores_actuales = {p.nombre.lower(): p for p in Proveedor.objects.all()}
            productos_actuales = {p.codigo_barras: p for p in Producto.objects.filter(codigo_barras__isnull=False, codigo_barras__ne='')}

            filas_confirmadas = []    # Verde - Coincidencia exacta de producto
            filas_para_revisar = []     # Amarillo/Rojo - Producto nuevo o proveedor dudoso
            filas_con_problemas = []  # Errores de formato

            for index, row in df.iterrows():
                # Normalizamos los datos leídos del Excel
                codigo_barras = str(row.get('codigo_barras', '')).strip()
                nombre_producto = str(row.get('nombre', '')).strip()
                proveedor_nombre = str(row.get('proveedor_nombre', '')).strip()
                cantidad = row.get('cantidad')

                # Verificación básica
                if not codigo_barras or not cantidad or not nombre_producto:
                    row['error'] = 'Falta código, cantidad o nombre.'
                    filas_con_problemas.append(row.to_dict())
                    continue

                fila_data = row.to_dict() # Convertimos la fila a un dict
                fila_data['index'] = index # Guardamos el índice para el formulario

                # --- INICIA LÓGICA DE COINCIDENCIA ---
                producto_existente = productos_actuales.get(codigo_barras)

                if producto_existente:
                    # --- CASO 1: COINCIDENCIA EXACTA (VERDE) ---
                    # El producto ya existe por código de barras. Solo vamos a cargar stock.
                    fila_data['tipo'] = 'stock'
                    fila_data['producto_id'] = producto_existente.id
                    fila_data['producto_nombre'] = producto_existente.nombre
                    filas_confirmadas.append(fila_data)

                else:
                    # --- CASO 2: PRODUCTO NUEVO (ROJO/AMARILLO) ---
                    # El producto no existe. Necesitamos crearlo.
                    fila_data['tipo'] = 'nuevo_producto'

                    # Ahora, revisemos al proveedor para sugerir
                    proveedor_sugerido = None
                    if proveedor_nombre:
                        # 2a: Coincidencia exacta de proveedor (ignorando mayúsculas)
                        proveedor_obj = proveedores_actuales.get(proveedor_nombre.lower())
                        if proveedor_obj:
                            proveedor_sugerido = {'id': proveedor_obj.id, 'nombre': proveedor_obj.nombre, 'similaridad': 100}
                        else:
                            # 2b: Coincidencia difusa (fuzzy) de proveedor
                            mejor_coincidencia = None
                            mayor_puntaje = 0
                            # Comparamos con todos los proveedores existentes
                            for nombre_existente, obj_existente in proveedores_actuales.items():
                                puntaje = fuzz.ratio(proveedor_nombre.lower(), nombre_existente)
                                if puntaje > mayor_puntaje:
                                    mayor_puntaje = puntaje
                                    mejor_coincidencia = obj_existente

                            # Si la similitud es alta (ej. > 85%), lo sugerimos
                            if mayor_puntaje > 85: 
                                proveedor_sugerido = {'id': mejor_coincidencia.id, 'nombre': mejor_coincidencia.nombre, 'similaridad': mayor_puntaje}

                    fila_data['proveedor_sugerido'] = proveedor_sugerido
                    filas_para_revisar.append(fila_data)

            # 3. Enviamos los datos analizados a la nueva plantilla de confirmación
            context = {
                'filas_confirmadas': filas_confirmadas,
                'filas_para_revisar': filas_para_revisar,
                'filas_con_problemas': filas_con_problemas,
                'sucursal_actual': sucursal_usuario,
                # Pasamos todas las categorías y proveedores para los <select> del formulario
                'todos_los_proveedores': Proveedor.objects.all().order_by('nombre'),
                'todas_las_categorias': Categoria.objects.all().order_by('nombre')
            }
            # Renderizamos la NUEVA plantilla de confirmación
            return render(request, 'core/confirmar_importacion_excel.html', context)

        except Exception as e:
            messages.error(request, f"Error al leer el archivo Excel: {e}")
            return redirect('importar_stock')

    # Si no es POST, solo muestra la página de subida
    return render(request, 'core/importar_stock.html')


@login_required
def procesar_importacion_excel(request):
    if request.method != 'POST':
        return redirect('importar_stock')

    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Error: Usuario sin sucursal asignada.")
        return redirect('dashboard')

    # 1. Agrupar todos los datos del formulario por su índice
    items_a_procesar = {}
    for key, value in request.POST.items():
        if key.startswith('item_'):
            # Dividimos la clave: item_INDICE_campo
            parts = key.split('_')
            index = parts[1]
            field_name = '_'.join(parts[2:])
            
            if index not in items_a_procesar:
                items_a_procesar[index] = {}
            items_a_procesar[index][field_name] = value

    items_cargados = 0
    productos_creados = 0
    proveedores_creados = 0

    try:
        # 2. Usamos una transacción. Si algo falla, se deshace todo.
        with transaction.atomic():
            for index, data in items_a_procesar.items():
                tipo = data.get('tipo')
                if not tipo: continue

                # Obtenemos los datos comunes
                cantidad = int(data.get('cantidad', 0))
                fecha_vencimiento_str = data.get('fecha_vencimiento')
                fecha_vencimiento = pd.to_datetime(fecha_vencimiento_str) if fecha_vencimiento_str and pd.notna(fecha_vencimiento_str) else None
                ubicacion = data.get('ubicacion', 'deposito')

                if cantidad <= 0: continue # Omitir si la cantidad es 0 o inválida

                producto_obj = None

                if tipo == 'stock':
                    # --- CASO 1: Solo cargar stock a producto existente ---
                    producto_obj = get_object_or_404(Producto, id=data['producto_id'])
                
                elif tipo == 'nuevo_producto':
                    # --- CASO 2: Crear producto nuevo ---
                    
                    # 2a. Gestionar Proveedor
                    proveedor_id_o_crear = data.get('proveedor_id')
                    proveedor_obj = None
                    if proveedor_id_o_crear:
                        if proveedor_id_o_crear.startswith('CREAR_NUEVO_'):
                            nombre_nuevo_prov = proveedor_id_o_crear.replace('CREAR_NUEVO_', '')
                            proveedor_obj, created = Proveedor.objects.get_or_create(
                                nombre__iexact=nombre_nuevo_prov,
                                defaults={'nombre': nombre_nuevo_prov}
                            )
                            if created: proveedores_creados += 1
                        else:
                            proveedor_obj = get_object_or_404(Proveedor, id=proveedor_id_o_crear)
                    
                    # 2b. Crear Producto
                    producto_obj = Producto.objects.create(
                        nombre=data['nombre'],
                        codigo_barras=data['codigo_barras'],
                        proveedor=proveedor_obj,
                        categoria_id=data['categoria_id'],
                        costo=Decimal(data.get('costo', 0)),
                        precio_venta=Decimal(data.get('precio_venta', 0)),
                        stock_minimo=5 # Default
                    )
                    productos_creados += 1

                # 3. Guardar el Stock (común a ambos casos)
                if producto_obj:
                    Stock.objects.create(
                        producto=producto_obj,
                        cantidad=cantidad,
                        fecha_vencimiento=fecha_vencimiento,
                        ubicacion=ubicacion,
                        sucursal=sucursal_usuario
                    )
                    items_cargados += 1

        # 4. Mostrar mensaje de éxito
        msg = f"¡Importación completada! {items_cargados} lotes de stock cargados. {productos_creados} productos nuevos creados. {proveedores_creados} proveedores nuevos creados."
        messages.success(request, msg)

    except Exception as e:
        messages.error(request, f"Ocurrió un error grave durante el procesamiento. No se guardó ningún dato. Error: {e}")

    return redirect('stock_detalle')


@login_required
def cargar_factura_ocr(request):
    if vision is None:
        messages.error(request, "Función no disponible (falta 'google-cloud-vision').")
        return redirect('dashboard')
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Necesitas sucursal asignada.")
        return redirect('dashboard')

    if request.method == 'POST' and request.FILES.get('imagen_factura'):
        try:
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'gcloud-credentials.json'
            client = vision.ImageAnnotatorClient()
            content = request.FILES['imagen_factura'].read()
            image = vision.Image(content=content)
            response = client.document_text_detection(image=image)
            if response.error.message: raise Exception(f'{response.error.message}\nVerifica API.')
            full_text = response.text_annotations[0].description if response.text_annotations else ""
        except Exception as e:
            messages.error(request, f"Error al procesar con IA: {e}")
            return redirect('cargar_factura_ocr')

        productos_encontrados = []
        lineas = full_text.split('\n')
        PALABRAS_FILTRO = ['total', 'subtotal', 'iva', 'pago', 'gracias', 'cuit', 'fecha', 'mesa', 'comensales', 'atendido', 'base', 'dto', 'descuento']

        for i, linea in enumerate(lineas):
            linea_limpia = linea.strip().lower()
            if not linea_limpia or any(palabra in linea_limpia for palabra in PALABRAS_FILTRO): continue

            cantidad, descripcion, costo = None, None, None
            # Patrón 1
            match = re.search(r'^\s*([\d,]+)\s*[xX]?\s*(.*?)(?:\s+\$?([\d,]+\.?\d*))?\s*$', linea)
            if match:
                try:
                    cantidad = int(match.group(1).replace(',', ''))
                    descripcion = match.group(2).strip()
                    costo_str = match.group(3)
                    if costo_str: costo = Decimal(costo_str.replace(',', '.'))
                    else:
                        if i + 1 < len(lineas):
                            match_precio_siguiente = re.search(r'^\s*\$?([\d,]+\.?\d*)\s*$', lineas[i+1])
                            if match_precio_siguiente: costo = Decimal(match_precio_siguiente.group(1).replace(',', '.'))
                except: continue
            # Patrón 2
            elif i + 1 < len(lineas):
                linea_siguiente = lineas[i+1]
                match_siguiente = re.search(r'^\s*([\d,]+)\s*(?:u|un|ud|und)?\s*x\s*\$?([\d,]+\.?\d*)', linea_siguiente, re.IGNORECASE)
                if match_siguiente:
                    try:
                        cantidad = int(match_siguiente.group(1).replace(',', ''))
                        descripcion = linea.strip()
                        costo_unitario = Decimal(match_siguiente.group(2).replace(',', '.'))
                        costo = costo_unitario * cantidad
                    except: continue

            if cantidad is not None and descripcion and cantidad > 0:
                producto_db = Producto.objects.filter(nombre__iexact=descripcion).first() or Producto.objects.filter(nombre__icontains=descripcion).first()
                costo_unitario_final = costo / cantidad if costo is not None and cantidad > 0 else Decimal('0.00')
                precio_venta_sugerido = None
                if producto_db and producto_db.categoria and producto_db.categoria.margen_ganancia_porcentaje > 0:
                    margen = producto_db.categoria.margen_ganancia_porcentaje / Decimal(100)
                    precio_venta_sugerido = (costo_unitario_final * (1 + margen)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                productos_encontrados.append({
                    'id_temporal': i, 'descripcion_factura': descripcion, 'cantidad_sugerida': cantidad,
                    'costo_sugerido': costo_unitario_final, 'producto_db': producto_db,
                    'precio_venta_sugerido': precio_venta_sugerido or (producto_db.precio_venta if producto_db else '')
                })

        context = {'productos_encontrados': productos_encontrados, 'todos_los_productos': Producto.objects.all(), 'texto_completo_ocr': full_text}
        return render(request, 'core/confirmar_factura_ocr.html', context)

    return render(request, 'core/cargar_factura_ocr.html')

@login_required
def guardar_factura_confirmada(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Error: No se pudo determinar la sucursal del usuario.")
        return redirect('dashboard')

    if request.method == 'POST':
        fecha_vencimiento = request.POST.get('fecha_vencimiento') or None
        ubicacion = request.POST.get('ubicacion', 'deposito')
        items = {}
        for key, value in request.POST.items():
             if '_' in key:
                 try: # Evitar errores si el ID no es numérico
                     parts = key.split('_')
                     item_id = parts[-1]
                     field_name = '_'.join(parts[:-1])
                     if item_id not in items: items[item_id] = {}
                     items[item_id][field_name] = value
                 except ValueError:
                     pass # Ignorar claves mal formadas

        items_cargados = 0
        try:
            with transaction.atomic():
                for item_id, data in items.items():
                    if not all(k in data for k in ('producto', 'cantidad', 'costo')) or not data['producto'] or not data['cantidad'] or not data['costo']:
                         print(f"Omitiendo item {item_id}, faltan datos: {data}")
                         continue
                    try:
                        producto_id = data['producto']
                        cantidad = int(data['cantidad'])
                        costo = Decimal(data['costo'])
                        precio_venta_str = data.get('precio_venta', '0')
                        precio_venta = Decimal(precio_venta_str) if precio_venta_str else Decimal('0') # Manejar string vacío
                    except (ValueError, TypeError):
                         messages.warning(request, f"Datos inválidos para item {item_id}. Omitido.")
                         continue

                    if cantidad <= 0 or costo < 0: continue

                    producto = get_object_or_404(Producto, id=producto_id)
                    producto.costo = costo
                    if precio_venta > 0: producto.precio_venta = precio_venta
                    elif producto.categoria and producto.categoria.margen_ganancia_porcentaje > 0:
                        margen = producto.categoria.margen_ganancia_porcentaje / Decimal(100)
                        precio_venta_sugerido = (costo * (1 + margen)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        producto.precio_venta = precio_venta_sugerido
                    producto.save()

                    Stock.objects.create(
                        producto=producto, cantidad=cantidad, fecha_vencimiento=fecha_vencimiento,
                        ubicacion=ubicacion, sucursal=sucursal_usuario
                    )
                    items_cargados += 1

            if items_cargados > 0: messages.success(request, f"¡Factura cargada! Se añadieron {items_cargados} items al stock.")
            else: messages.warning(request, "No se cargaron items válidos.")
            return redirect('stock_detalle')
        except Exception as e:
            messages.error(request, f"Ocurrió un error al guardar: {e}")
            return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

    return redirect('dashboard')

@login_required
def descargar_plantilla_excel(request):
    # Definimos las columnas exactas que espera nuestro importador
    columnas = [
        'codigo_barras', 'cantidad', 'nombre', 'costo', 
        'precio_venta', 'fecha_vencimiento', 'ubicacion', 'proveedor_nombre'
    ]

    # Creamos un DataFrame de pandas vacío solo con las cabeceras
    df = pd.DataFrame(columns=columnas)

    # Creamos un "archivo en memoria" para guardar el Excel
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Plantilla', index=False)

    # Preparamos la respuesta HTTP para que el navegador descargue el archivo
    buffer.seek(0)
    response = HttpResponse(
        buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="plantilla_stock.xlsx"'
    return response


# ==============================================================================
# VISTAS DE GESTIÓN (Proveedores, Productos, Categorías) - ¡CON PERMISOS!
# ==============================================================================
@login_required
def listar_proveedores(request):
    proveedores = Proveedor.objects.all().order_by('nombre')
    return render(request, 'core/listar_proveedores.html', {'proveedores': proveedores})

@login_required
def crear_proveedor(request):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para crear proveedores.")
        return redirect('listar_proveedores')
    # --- FIN PERMISO ---
    if request.method == 'POST':
        dia_semana = request.POST.get('dia_semana_reparto')
        frecuencia = request.POST.get('frecuencia_reparto')
        Proveedor.objects.create(
            nombre=request.POST['nombre'], telefono=request.POST.get('telefono', ''),
            email=request.POST.get('email', ''),
            dia_semana_reparto=int(dia_semana) if dia_semana else None,
            frecuencia_reparto=int(frecuencia) if frecuencia else None
        )
        messages.success(request, '¡Proveedor creado!')
        return redirect('listar_proveedores')
    return render(request, 'core/form_proveedor.html')

@login_required
def editar_proveedor(request, proveedor_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para editar proveedores.")
        return redirect('listar_proveedores')
    # --- FIN PERMISO ---
    proveedor = get_object_or_404(Proveedor, id=proveedor_id)
    if request.method == 'POST':
        proveedor.nombre = request.POST['nombre']
        proveedor.telefono = request.POST.get('telefono', '')
        proveedor.email = request.POST.get('email', '')
        dia_semana = request.POST.get('dia_semana_reparto')
        frecuencia = request.POST.get('frecuencia_reparto')
        proveedor.dia_semana_reparto = int(dia_semana) if dia_semana else None
        proveedor.frecuencia_reparto = int(frecuencia) if frecuencia else None
        proveedor.save()
        messages.success(request, '¡Proveedor actualizado!')
        return redirect('listar_proveedores')
    return render(request, 'core/form_proveedor.html', {'proveedor': proveedor})

@login_required
def eliminar_proveedor(request, proveedor_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para eliminar proveedores.")
        return redirect('listar_proveedores')
    # --- FIN PERMISO ---
    proveedor = get_object_or_404(Proveedor, id=proveedor_id)
    if request.method == 'POST':
        proveedor.delete()
        messages.success(request, '¡Proveedor eliminado!')
    return redirect('listar_proveedores')

@login_required
def detalle_proveedor(request, proveedor_id):
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('listar_proveedores')

    proveedor = get_object_or_404(Proveedor, id=proveedor_id)

    # Pestaña 1: Productos
    productos_del_proveedor = Producto.objects.filter(proveedor=proveedor).order_by('nombre')

    # Pestaña 2: Cuenta Corriente
    facturas_pendientes = FacturaProveedor.objects.filter(proveedor=proveedor, pagada=False).order_by('fecha_vencimiento')
    pagos_realizados = PagoProveedor.objects.filter(proveedor=proveedor).order_by('-fecha')[:20] # Últimos 20 pagos

    context = {
        'proveedor': proveedor,
        'productos_del_proveedor': productos_del_proveedor,
        'facturas_pendientes': facturas_pendientes,
        'pagos_realizados': pagos_realizados,
        'saldo_deudor': proveedor.saldo_actual # Usamos el campo del modelo
    }
    return render(request, 'core/detalle_proveedor.html', context)

@login_required
def registrar_factura_proveedor(request):
    if not request.user.is_superuser:
        return redirect('dashboard')

    if request.method == 'POST':
        proveedor_id = request.POST.get('proveedor')
        monto_total_str = request.POST.get('monto_total')
        sucursal_id = request.POST.get('sucursal') # De qué sucursal es la factura

        try:
            proveedor = get_object_or_404(Proveedor, id=proveedor_id)
            sucursal = get_object_or_404(Sucursal, id=sucursal_id)
            monto = Decimal(monto_total_str)

            with transaction.atomic():
                # 1. Creamos la factura
                FacturaProveedor.objects.create(
                    proveedor=proveedor,
                    sucursal=sucursal,
                    numero_factura=request.POST.get('numero_factura'),
                    monto_total=monto,
                    fecha_factura=request.POST.get('fecha_factura') or timezone.now().date(),
                    fecha_vencimiento=request.POST.get('fecha_vencimiento') or None,
                    pagada=False
                )
                # 2. Actualizamos el saldo del proveedor (aumenta nuestra deuda)
                proveedor.saldo_actual += monto
                proveedor.save()

            messages.success(request, f"Factura de {proveedor.nombre} por ${monto} registrada.")
            return redirect('detalle_proveedor', proveedor_id=proveedor.id)

        except Exception as e:
            messages.error(request, f"Error al registrar la factura: {e}")

    # Si es GET, mostramos el formulario
    proveedores = Proveedor.objects.all()
    sucursales = Sucursal.objects.all()
    return render(request, 'core/form_factura_proveedor.html', {
        'proveedores': proveedores,
        'sucursales': sucursales
    })

@login_required
def registrar_pago_proveedor(request):
    if not request.user.is_superuser or request.method != 'POST':
        return redirect('dashboard')

    proveedor_id = request.POST.get('proveedor_id')
    monto_str = request.POST.get('monto')
    sucursal = obtener_sucursal_usuario(request) # Asumimos que el pago se hace desde la sucursal del admin

    proveedor = get_object_or_404(Proveedor, id=proveedor_id)

    try:
        monto = Decimal(monto_str)
        if monto <= 0: raise ValueError("El monto debe ser positivo.")

        with transaction.atomic():
            # 1. Registramos el pago
            PagoProveedor.objects.create(
                proveedor=proveedor, 
                sucursal=sucursal if sucursal else Sucursal.objects.first(), # Fallback por si admin no tiene sucursal
                monto=monto
            )
            # 2. Actualizamos el saldo del proveedor (disminuye nuestra deuda)
            proveedor.saldo_actual -= monto
            proveedor.save()

        messages.success(request, f"Se registró un pago de ${monto} a {proveedor.nombre}.")
    except Exception as e:
        messages.error(request, f"Error al registrar el pago: {e}")

    return redirect('detalle_proveedor', proveedor_id=proveedor.id)

@login_required
def listar_productos(request):
    productos = Producto.objects.select_related('proveedor').all().order_by('nombre')
    return render(request, 'core/listar_productos.html', {'productos': productos})

@login_required
def crear_producto(request):
    if request.method == 'POST':
        # --- CORRECCIÓN AQUÍ ---
        # Movemos la lógica para obtener datos DENTRO del bloque try
        # y cerramos el paréntesis de 'codigo_barras'

        codigo_barras = request.POST.get('codigo_barras') or None # Guardar None si está vacío

        try:
            prov_id = request.POST.get('proveedor')
            cat_id = request.POST.get('categoria')

            Producto.objects.create(
                nombre=request.POST['nombre'],
                codigo_barras=codigo_barras, 
                proveedor_id=int(prov_id) if prov_id else None,
                categoria_id=int(cat_id) if cat_id else None,
                costo=request.POST['costo'],
                precio_venta=request.POST['precio_venta'],
                stock_minimo=request.POST.get('stock_minimo', 5) or 5,
                es_perecedero=request.POST.get('es_perecedero') == 'on',
                es_favorito=request.POST.get('es_favorito') == 'on'
            )
            messages.success(request, '¡Producto creado!')
            return redirect('listar_productos')

        except IntegrityError: 
            messages.error(request, f'Error: Ya existe un producto con el Código de Barras "{codigo_barras}".')
        except Exception as e:
            messages.error(request, f'Error inesperado: {e}')

        # --- FIN DE LA CORRECCIÓN ---

    # --- Lógica GET (se mantiene igual) ---
    proveedores = Proveedor.objects.all().order_by('nombre')
    categorias = Categoria.objects.all().order_by('nombre')

    categorias_json = json.dumps(
        {cat.id: float(cat.margen_ganancia_porcentaje) for cat in categorias}
    )

    context = {
        'proveedores': proveedores,
        'categorias': categorias,
        'categorias_json': categorias_json,
    }
    return render(request, 'core/form_producto.html', context)


@login_required
def editar_producto(request, producto_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para editar productos.")
        return redirect('listar_productos')
    # --- FIN PERMISO ---
    producto = get_object_or_404(Producto, id=producto_id)
    if request.method == 'POST':
        producto.nombre = request.POST['nombre']
        producto.codigo_barras = request.POST.get('codigo_barras')
        producto.proveedor_id = request.POST.get('proveedor') or None
        producto.categoria_id = request.POST.get('categoria') or None
        producto.costo = request.POST['costo']
        producto.precio_venta = request.POST['precio_venta']
        producto.stock_minimo = request.POST.get('stock_minimo', 5) or 5
        producto.save()
        producto.es_perecedero = request.POST.get('es_perecedero') == 'on'
        producto.es_favorito = request.POST.get('es_favorito') == 'on'
        producto.save()
        messages.success(request, '¡Producto actualizado!')
        return redirect('listar_productos')
    
    proveedores = Proveedor.objects.all().order_by('nombre')
    categorias = Categoria.objects.all().order_by('nombre')
    categorias_json = json.dumps(
        {cat.id: float(cat.margen_ganancia_porcentaje) for cat in categorias}
    )
    return render(request, 'core/form_producto.html', {'producto': producto, 'proveedores': proveedores, 'categorias': categorias,'categorias_json': categorias_json})

@login_required
def eliminar_producto(request, producto_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para eliminar productos.")
        return redirect('listar_productos')
    # --- FIN PERMISO ---
    producto = get_object_or_404(Producto, id=producto_id)
    if request.method == 'POST':
        producto.delete()
        messages.success(request, '¡Producto eliminado!')
    return redirect('listar_productos')

@login_required
def listar_categorias(request):
    categorias = Categoria.objects.all().order_by('nombre')
    return render(request, 'core/listar_categorias.html', {'categorias': categorias})

@login_required
def crear_categoria(request):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para crear categorías.")
        return redirect('listar_categorias')
    # --- FIN PERMISO ---
    if request.method == 'POST':
        Categoria.objects.create(
            nombre=request.POST['nombre'],
            margen_ganancia_porcentaje=request.POST.get('margen_ganancia_porcentaje', 0) or 0
        )
        messages.success(request, '¡Categoría creada!')
        return redirect('listar_categorias')
    return render(request, 'core/form_categoria.html')

@login_required
def editar_categoria(request, categoria_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para editar categorías.")
        return redirect('listar_categorias')
    # --- FIN PERMISO ---
    categoria = get_object_or_404(Categoria, id=categoria_id)
    if request.method == 'POST':
        categoria.nombre = request.POST['nombre']
        categoria.margen_ganancia_porcentaje = request.POST.get('margen_ganancia_porcentaje', 0) or 0
        categoria.save()
        messages.success(request, '¡Categoría actualizada!')
        return redirect('listar_categorias')
    return render(request, 'core/form_categoria.html', {'categoria': categoria})

@login_required
def eliminar_categoria(request, categoria_id):
    # --- SOLO SUPERUSUARIO ---
    if not request.user.is_superuser:
        messages.error(request, "No tienes permiso para eliminar categorías.")
        return redirect('listar_categorias')
    # --- FIN PERMISO ---
    categoria = get_object_or_404(Categoria, id=categoria_id)
    if request.method == 'POST':
        categoria.delete()
        messages.success(request, '¡Categoría eliminada!')
    return redirect('listar_categorias')

# ==============================================================================
# VISTAS DE GESTIÓN DE CLIENTES Y CUENTA CORRIENTE
# ==============================================================================

@login_required
def listar_clientes(request):
    # Mostramos todos los clientes. Podríamos filtrar por sucursal si fuera necesario.
    clientes = Cliente.objects.all().order_by('nombre_completo')
    return render(request, 'core/listar_clientes.html', {'clientes': clientes})

@login_required
def crear_cliente(request):
    if request.method == 'POST':
        limite_credito = request.POST.get('limite_credito', 0)

        Cliente.objects.create(
            nombre_completo=request.POST['nombre_completo'],
            dni=request.POST.get('dni'),
            telefono=request.POST.get('telefono'),
            limite_credito=limite_credito if request.user.is_superuser else 0, # Solo admin pone límite
            saldo_actual=0
        )
        messages.success(request, '¡Cliente creado exitosamente!')
        return redirect('listar_clientes')

    return render(request, 'core/form_cliente.html')

@login_required
def editar_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, id=cliente_id)

    if request.method == 'POST':
        cliente.nombre_completo = request.POST['nombre_completo']
        cliente.dni = request.POST.get('dni')
        cliente.telefono = request.POST.get('telefono')

        if request.user.is_superuser: # Solo admin puede cambiar el límite
            cliente.limite_credito = request.POST.get('limite_credito', 0)

        cliente.save()
        messages.success(request, '¡Cliente actualizado exitosamente!')
        return redirect('listar_clientes')

    return render(request, 'core/form_cliente.html', {'cliente': cliente})

@login_required
def estado_cuenta_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, id=cliente_id)

    # Obtenemos todas las transacciones (ventas fiadas y pagos)
    ventas_fiadas = Venta.objects.filter(
        cliente=cliente, 
        metodo_pago='cuenta_corriente'
    ).order_by('-fecha_hora')

    pagos_realizados = PagoCliente.objects.filter(cliente=cliente).order_by('-fecha')

    # Combinar y ordenar transacciones por fecha (opcional pero recomendado para un resumen real)
    # Por ahora, las pasamos separadas para mostrarlas en dos tablas.

    context = {
        'cliente': cliente,
        'ventas_fiadas': ventas_fiadas,
        'pagos_realizados': pagos_realizados,
    }
    return render(request, 'core/estado_cuenta_cliente.html', context)

@login_required
def registrar_pago_cliente(request):
    if request.method != 'POST':
        return redirect('listar_clientes')

    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Usuario sin sucursal asignada para registrar un pago.")
        return redirect('listar_clientes')

    cliente_id = request.POST.get('cliente_id')
    monto_str = request.POST.get('monto')
    cliente = get_object_or_404(Cliente, id=cliente_id)

    try:
        monto = Decimal(monto_str)
        if monto <= 0:
            raise ValueError("El monto debe ser positivo.")

        with transaction.atomic():
            # 1. Registramos el pago
            PagoCliente.objects.create(
                cliente=cliente, 
                sucursal=sucursal_usuario, 
                monto=monto
            )
            # 2. Actualizamos el saldo del cliente
            cliente.saldo_actual -= monto
            cliente.save()

        messages.success(request, f"Se registró un pago de ${monto} para {cliente.nombre_completo}.")

    except (ValueError, TypeError):
        messages.error(request, "Error: El monto ingresado no es válido.")
    except Exception as e:
        messages.error(request, f"Error inesperado: {e}")

    return redirect('estado_cuenta_cliente', cliente_id=cliente.id)

@login_required
def api_buscar_clientes(request):
    query = request.GET.get('term', '')
    # Filtramos por nombre o DNI
    clientes = Cliente.objects.filter(
        Q(nombre_completo__icontains=query) | Q(dni__icontains=query)
    )[:10]

    resultados = [
        {'id': c.id, 'nombre': c.nombre_completo, 'saldo': c.saldo_actual, 'limite': c.limite_credito} 
        for c in clientes
    ]
    return JsonResponse(resultados, safe=False)



# ==============================================================================
# VISTAS DE GESTIÓN DE ENVASES (Solo Superuser)
# ==============================================================================
@login_required
def listar_envases(request):
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('dashboard')
    envases = EnvaseRetornable.objects.all().order_by('nombre')
    return render(request, 'core/listar_envases.html', {'envases': envases})

@login_required
def crear_envase(request):
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('listar_envases')
    if request.method == 'POST':
        EnvaseRetornable.objects.create(
            nombre=request.POST['nombre'],
            valor_deposito=request.POST.get('valor_deposito', 0)
        )
        messages.success(request, '¡Envase creado exitosamente!')
        return redirect('listar_envases')
    return render(request, 'core/form_envase.html')

@login_required
def editar_envase(request, envase_id):
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('listar_envases')
    envase = get_object_or_404(EnvaseRetornable, id=envase_id)
    if request.method == 'POST':
        envase.nombre = request.POST['nombre']
        envase.valor_deposito = request.POST.get('valor_deposito', 0)
        envase.save()
        messages.success(request, '¡Envase actualizado exitosamente!')
        return redirect('listar_envases')
    return render(request, 'core/form_envase.html', {'envase': envase})

@login_required
def eliminar_envase(request, envase_id):
    if not request.user.is_superuser:
        messages.error(request, "Acceso denegado.")
        return redirect('listar_envases')
    envase = get_object_or_404(EnvaseRetornable, id=envase_id)
    if request.method == 'POST':
        envase.delete()
        messages.success(request, '¡Envase eliminado exitosamente!')
    return redirect('listar_envases')

# ==============================================================================
# VISTAS DE REPORTES Y ANÁLISIS
# ==============================================================================
@login_required
def analisis_canasta(request):
    # 1. Obtener todas las ventas y sus detalles
    ventas = Venta.objects.prefetch_related('detalles__producto').all()

    # 2. Convertir las ventas en "transacciones" (listas de nombres de productos)
    transacciones = []
    for venta in ventas:
        # Incluir solo ventas con más de un producto
        if venta.detalles.count() > 1:
            productos_en_venta = [detalle.producto.nombre for detalle in venta.detalles.all()]
            transacciones.append(productos_en_venta)

    resultados_apyori = []
    if transacciones:
        # 3. Ejecutar el algoritmo Apriori
        # Ajusta min_support y min_confidence según necesites (más altos = reglas más fuertes pero menos cantidad)
        reglas = apriori(transacciones, min_support=0.01, min_confidence=0.1, min_lift=1.1, min_length=2)

        # 4. Formatear los resultados para mostrarlos
        for regla in reglas:
            for item_set in regla.ordered_statistics:
                # Mostrar solo reglas simples A -> B
                if len(item_set.items_base) == 1 and len(item_set.items_add) == 1:
                    item_base = list(item_set.items_base)[0]
                    item_add = list(item_set.items_add)[0]
                    soporte = round(regla.support * 100, 2) # % de todas las transacciones
                    confianza = round(item_set.confidence * 100, 2) # % de veces que B se compra si se compra A

                    resultados_apyori.append({
                        'base': item_base,
                        'add': item_add,
                        'soporte': soporte,
                        'confianza': confianza,
                    })

    # Ordenamos por confianza (las reglas más fuertes primero)
    resultados_apyori.sort(key=lambda x: x['confianza'], reverse=True)

    return render(request, 'core/analisis_canasta.html', {'reglas': resultados_apyori})


@login_required
def sugerencias_compra(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario and not request.user.is_superuser:
        messages.error(request, "Necesitas una sucursal asignada.")
        return redirect('dashboard')

    # Obtenemos productos con su stock total (global o por sucursal) y proveedor
    productos = Producto.objects.select_related('proveedor').annotate(
        stock_total=Sum('lotes__cantidad', filter=Q(lotes__sucursal=sucursal_usuario)) if sucursal_usuario else Sum('lotes__cantidad')
    ).filter(stock_total__isnull=False) # Solo productos con stock calculado

    # Filtramos los que están bajo el mínimo
    productos_a_pedir = productos.filter(stock_total__lt=F('stock_minimo'))

    # Agrupamos por proveedor
    sugerencias_por_proveedor = {}
    for producto in productos_a_pedir:
        # Calculamos cuánto pedir (simple: para llegar al mínimo + un poco más)
        cantidad_a_pedir = (producto.stock_minimo - producto.stock_total) + (producto.stock_minimo // 2) # Pedir para llegar al minimo + 50%

        proveedor_nombre = producto.proveedor.nombre if producto.proveedor else "Sin Proveedor Asignado"

        if proveedor_nombre not in sugerencias_por_proveedor:
            sugerencias_por_proveedor[proveedor_nombre] = []

        sugerencias_por_proveedor[proveedor_nombre].append({
            'nombre': producto.nombre,
            'stock_actual': producto.stock_total,
            'stock_minimo': producto.stock_minimo,
            'cantidad_sugerida': cantidad_a_pedir
        })

    context = {
        'sugerencias': sugerencias_por_proveedor,
        'sucursal_actual': sucursal_usuario
    }
    return render(request, 'core/sugerencias_compra.html', context)

# ==============================================================================
# VISTA DE REPORTES Y CIERRE DE CAJA
# ==============================================================================
@login_required
def reportes_dashboard(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not request.user.is_superuser:
        if not sucursal_usuario:
            messages.error(request, "Tu usuario no está asignado a ninguna sucursal.")
            return redirect('dashboard')
    
    # --- 1. FILTRADO DE FECHAS ---
    fecha_inicio_str = request.GET.get('fecha_inicio', timezone.now().strftime('%Y-%m-%d'))
    fecha_fin_str = request.GET.get('fecha_fin', timezone.now().strftime('%Y-%m-%d'))
    
    try:
        fecha_inicio = datetime.strptime(fecha_inicio_str, '%Y-%m-%d').date()
        # Ajuste: Hacemos que la fecha fin incluya el día completo (hasta las 23:59:59)
        fecha_fin_dt = datetime.strptime(fecha_fin_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1)
    except ValueError:
        messages.error(request, "Formato de fecha inválido.")
        return redirect('reportes_dashboard')

    # --- 2. FILTRADO DE SUCURSAL ---
    sucursal_id_filtro = request.GET.get('sucursal_id')
    sucursal_seleccionada = None
    
    if request.user.is_superuser:
        if sucursal_id_filtro:
            sucursal_seleccionada = get_object_or_404(Sucursal, id=sucursal_id_filtro)
    else:
        sucursal_seleccionada = sucursal_usuario

    # --- 3. QUERIES DE DATOS ---
    
    # Base de consultas filtradas por fecha
    ventas_query = Venta.objects.filter(fecha_hora__range=(fecha_inicio, fecha_fin_dt))
    pagos_clientes_query = PagoCliente.objects.filter(fecha__range=(fecha_inicio, fecha_fin_dt))
    pagos_proveedores_query = PagoProveedor.objects.filter(fecha__range=(fecha_inicio, fecha_fin_dt))
    
    if sucursal_seleccionada:
        ventas_query = ventas_query.filter(sucursal=sucursal_seleccionada)
        pagos_clientes_query = pagos_clientes_query.filter(sucursal=sucursal_seleccionada)
        pagos_proveedores_query = pagos_proveedores_query.filter(sucursal=sucursal_seleccionada)

    # A. INGRESOS REALES (Dinero que entró a la caja/banco)
    
    # A1. Ventas (CORRECCIÓN: TRADUCCIÓN DE NOMBRES)
    # Obtenemos los datos crudos agrupados
    datos_brutos = ventas_query.exclude(metodo_pago='cuenta_corriente').values('metodo_pago').annotate(
        total=Sum('total')
    ).order_by('metodo_pago')
    
    # Creamos un diccionario para traducir (ej: 'efectivo' -> 'Efectivo')
    nombres_metodos = dict(Venta.METODO_PAGO_CHOICES)
    
    ingresos_por_metodo = []
    for dato in datos_brutos:
        codigo = dato['metodo_pago']
        # Obtenemos el nombre legible. Si no existe (por datos viejos vacíos), ponemos "Sin especificar"
        nombre_legible = nombres_metodos.get(codigo, "Sin especificar") if codigo else "Sin especificar"
        
        ingresos_por_metodo.append({
            'nombre_metodo': nombre_legible, # Usaremos esto en el HTML
            'total': dato['total']
        })

    # A2. Cobros de Cuentas Corrientes (Fiado)
    total_cobros_fiado = pagos_clientes_query.aggregate(total=Sum('monto'))['total'] or 0

    # B. SALIDAS REALES
    total_pagos_proveedor = pagos_proveedores_query.aggregate(total=Sum('monto'))['total'] or 0

    # C. MOVIMIENTOS A CRÉDITO (No afectan la caja)
    total_ventas_fiadas = ventas_query.filter(metodo_pago='cuenta_corriente').aggregate(total=Sum('total'))['total'] or 0

    # D. BALANCES GENERALES (Totales históricos)
    balance_clientes = Cliente.objects.aggregate(total=Sum('saldo_actual'))['total'] or 0
    balance_proveedores = Proveedor.objects.aggregate(total=Sum('saldo_actual'))['total'] or 0
    
    # E. HISTORIAL DE MOVIMIENTOS
    ventas_listado = ventas_query.order_by('-fecha_hora')[:50]
    pagos_clientes_listado = pagos_clientes_query.order_by('-fecha')[:50]
    pagos_proveedores_listado = pagos_proveedores_query.order_by('-fecha')[:50]
    
    context = {
        'ingresos_por_metodo': ingresos_por_metodo, # Lista procesada y traducida
        'total_cobros_fiado': total_cobros_fiado,
        'total_pagos_proveedor': total_pagos_proveedor,
        'total_ventas_fiadas': total_ventas_fiadas,
        
        'balance_clientes': balance_clientes,
        'balance_proveedores': balance_proveedores,
        
        'ventas_listado': ventas_listado,
        'pagos_clientes_listado': pagos_clientes_listado,
        'pagos_proveedores_listado': pagos_proveedores_listado,
        
        'fecha_inicio': fecha_inicio_str,
        'fecha_fin': fecha_fin_str,
        'sucursal_seleccionada': sucursal_seleccionada,
        'todas_las_sucursales': Sucursal.objects.all() if request.user.is_superuser else None,
    }
    return render(request, 'core/reportes_dashboard.html', context)

@login_required
def cerrar_turno(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Usuario sin sucursal asignada.")
        return redirect('dashboard')

    # 1. Encontrar la fecha del último cierre para esta sucursal
    ultimo_cierre = CierreTurno.objects.filter(sucursal=sucursal_usuario).order_by('-fecha_cierre_turno').first()
    fecha_inicio = ultimo_cierre.fecha_cierre_turno if ultimo_cierre else timezone.make_aware(datetime.min) # Si no hay cierres, tomar todo
    fecha_fin = timezone.now()

    # 2. Obtener todos los movimientos desde el último cierre
    ventas = Venta.objects.filter(sucursal=sucursal_usuario, fecha_hora__gt=fecha_inicio)
    pagos_clientes = PagoCliente.objects.filter(sucursal=sucursal_usuario, fecha__gt=fecha_inicio)
    pagos_proveedores = PagoProveedor.objects.filter(sucursal=sucursal_usuario, fecha__gt=fecha_inicio)

    # 3. Calcular totales
    total_efectivo = ventas.filter(metodo_pago='efectivo').aggregate(total=Sum('total'))['total'] or 0
    total_tarjeta = ventas.filter(metodo_pago__in=['debito', 'credito']).aggregate(total=Sum('total'))['total'] or 0
    total_qr = ventas.filter(metodo_pago='qr').aggregate(total=Sum('total'))['total'] or 0
    total_cobros = pagos_clientes.aggregate(total=Sum('monto'))['total'] or 0
    total_pagos = pagos_proveedores.aggregate(total=Sum('monto'))['total'] or 0

    # 4. Calcular el dinero que DEBERÍA HABER en caja (efectivo)
    caja_calculada = total_efectivo + total_cobros - total_pagos

    if request.method == 'POST':
        monto_declarado_str = request.POST.get('monto_en_caja_declarado')
        try:
            monto_declarado = Decimal(monto_declarado_str)

            # Guardamos el cierre
            nuevo_cierre = CierreTurno.objects.create(
                sucursal=sucursal_usuario,
                usuario_cierre=request.user,
                fecha_inicio_turno=fecha_inicio,
                fecha_cierre_turno=fecha_fin,
                total_ventas_efectivo=total_efectivo,
                total_ventas_tarjeta=total_tarjeta,
                total_ventas_qr=total_qr,
                total_cobros_fiado=total_cobros,
                total_pagos_proveedor=total_pagos,
                monto_en_caja_declarado=monto_declarado
            )

            messages.success(request, f"Cierre de turno guardado. Diferencia de caja: ${nuevo_cierre.diferencia_caja}")
            return redirect('dashboard')

        except (ValueError, TypeError):
            messages.error(request, "Monto declarado inválido.")

    context = {
        'fecha_inicio_turno': fecha_inicio,
        'total_efectivo': total_efectivo,
        'total_tarjeta': total_tarjeta,
        'total_qr': total_qr,
        'total_cobros_fiado': total_cobros,
        'total_pagos_proveedor': total_pagos,
        'caja_calculada': caja_calculada,
    }
    return render(request, 'core/cerrar_turno.html', context)