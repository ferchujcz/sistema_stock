# core/views.py
import time
from django.contrib.auth import logout
from fuzzywuzzy import fuzz
import io
from django.http import HttpResponse
import json
from .forms import ProductoForm
from .models import Configuracion
from django.conf import settings
from django.db.models.functions import Abs, TruncDate

# --- Imports de Django ---
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.dateparse import parse_date
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.contrib import messages
from django.utils import timezone
from django.db.models import Sum, Count,Case, When, IntegerField, Min, F, Q
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from .models import SesionEscaneo
from apyori import apriori
from django.db.models.functions import Coalesce
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib.auth.mixins import UserPassesTestMixin
# --- Imports de Python ---
import json
import os
import re
from datetime import timedelta,datetime
from decimal import Decimal, ROUND_HALF_UP
import google.generativeai as genai
from PIL import Image, ImageEnhance

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


def obtener_sucursal_usuario(request):
    user = request.user
    if not user.is_authenticated:
        return None

    es_admin = user.is_superuser or (hasattr(user, 'perfilusuario') and user.perfilusuario.rol == 'admin')

    if es_admin:
        # 1. Intentamos ver si eligió algo manual en el menú de la barra superior
        sucursal_id = request.session.get('sucursal_seleccionada_id')
        if sucursal_id:
            sucursal = Sucursal.objects.filter(id=sucursal_id).first()
            if sucursal: return sucursal
        
        # 2. Plan B automático: usamos la sucursal asignada a su perfil de usuario
        if hasattr(user, 'perfilusuario') and user.perfilusuario.sucursal:
            return user.perfilusuario.sucursal
            
        return None # Solo si no tiene perfil ni seleccionó nada
    
    # Si es empleado común, va directo a su sucursal fija
    return user.perfilusuario.sucursal if hasattr(user, 'perfilusuario') else None
# ==============================================================================
# VISTA PRINCIPAL (DASHBOARD)
# ==============================================================================
@login_required
def dashboard(request):
    context = {}
    usuario = request.user
    hoy = timezone.now().date()
    sucursal_usuario = obtener_sucursal_usuario(request)

  # --- 1. CÁLCULO DE GRÁFICO (Ventas últimos 7 días OPTIMIZADO) ---
    fecha_limite = hoy - timedelta(days=6)
    
    # Preparamos la consulta base
    ventas_semana_query = Venta.objects.filter(fecha_hora__date__gte=fecha_limite)
    if sucursal_usuario:
        ventas_semana_query = ventas_semana_query.filter(sucursal=sucursal_usuario)

    # MAGIA: Agrupamos y sumamos todo en 1 solo viaje a la base de datos
    ventas_por_dia = ventas_semana_query.annotate(
        dia=TruncDate('fecha_hora')
    ).values('dia').annotate(
        total_dia=Sum('total')
    ).order_by('dia')

    # Convertimos la respuesta en un diccionario rápido {fecha: total}
    ventas_dict = {v['dia']: float(v['total_dia']) for v in ventas_por_dia if v['dia']}

    ventas_semana_labels = []
    ventas_semana_data = []
    
    # Armamos las listas para el gráfico rellenando con 0 los días sin ventas
    for i in range(6, -1, -1):
        dia_actual = hoy - timedelta(days=i)
        ventas_semana_labels.append(dia_actual.strftime('%d/%m'))
        ventas_semana_data.append(ventas_dict.get(dia_actual, 0.0))

    context['ventas_labels'] = json.dumps(ventas_semana_labels)
    context['ventas_data'] = json.dumps(ventas_semana_data)
    context['total_ventas_semana'] = sum(ventas_semana_data)
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
    # --- ESCUDO DE SEGURIDAD (Permite Superuser y Admin de Local) ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ----------------------------------------------------------------

    sucursal = get_object_or_404(Sucursal, id=sucursal_id)
    hoy = timezone.now().date()
    hace_30_dias = hoy - timedelta(days=30)

    # 1. Traemos la lista de productos con su stock anotado (Un solo viaje a la BD)
    productos_con_stock = Producto.objects.filter(
        lotes__cantidad__gt=0,
        lotes__sucursal=sucursal
    ).annotate(
        total_gondola=Sum(Case(When(lotes__ubicacion='gondola', lotes__sucursal=sucursal, then='lotes__cantidad'), default=0, output_field=IntegerField())),
        total_deposito=Sum(Case(When(lotes__ubicacion='deposito', lotes__sucursal=sucursal, then='lotes__cantidad'), default=0, output_field=IntegerField())),
        vencimiento_proximo=Min('lotes__fecha_vencimiento', filter=Q(lotes__sucursal=sucursal))
    ).distinct()

    # --- OPTIMIZACIÓN: EL MAPA DE VENTAS (Elimina el N+1) ---
    # Traemos TODAS las ventas de esta sucursal en un solo viaje gigante a EEUU
    ventas_dict = DetalleVenta.objects.filter(
        venta__sucursal=sucursal,
        venta__fecha_hora__gte=hace_30_dias
    ).values('producto_id').annotate(total=Sum('cantidad'))
    
    # Creamos el mapa en la memoria RAM del servidor en Alemania
    ventas_map = {item['producto_id']: item['total'] for item in ventas_dict}
    # -------------------------------------------------------

    info_consolidada = []
    for producto in productos_con_stock:
        dias_para_vencer = None
        if producto.vencimiento_proximo:
            dias_para_vencer = (producto.vencimiento_proximo - hoy).days

        # Búsqueda instantánea en la RAM del servidor
        ventas_30_dias = ventas_map.get(producto.id, 0)
        velocidad_venta = ventas_30_dias / 30.0 if ventas_30_dias > 0 else 0 

        en_riesgo = False
        stock_total = (producto.total_gondola or 0) + (producto.total_deposito or 0)
        if velocidad_venta > 0 and dias_para_vencer is not None and dias_para_vencer > 0:
            dias_de_stock_restante = stock_total / velocidad_venta
            if dias_de_stock_restante > dias_para_vencer:
                en_riesgo = True

        info_consolidada.append({
            'producto': producto, 
            'total_gondola': producto.total_gondola,
            'total_deposito': producto.total_deposito,
            'vencimiento_proximo': producto.vencimiento_proximo,
            'dias_para_vencer': dias_para_vencer,
            'dias_para_vencer_abs': abs(dias_para_vencer) if dias_para_vencer is not None else None,
            'en_riesgo': en_riesgo, 
            'velocidad_venta': round(velocidad_venta, 2)
        })

    info_consolidada.sort(key=lambda x: (x['dias_para_vencer'] is None, x['dias_para_vencer'] if x['dias_para_vencer'] is not None else float('inf')))
    
    return render(request, 'core/stock_detalle.html', {
        'info_consolidada': info_consolidada,
        'sucursal_seleccionada': sucursal 
    })

@login_required
def stock_detalle(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario and not request.user.is_superuser:
        messages.error(request, "Tu usuario no está asignado a ninguna sucursal.")
        return render(request, 'core/stock_detalle.html', {'page_obj': []})

    hoy = timezone.now().date()
    hace_30_dias = hoy - timedelta(days=30)
    
    filtro = request.GET.get('filtro', 'todos')
    query = request.GET.get('q', '')

    # 1. BASE: Traer TODOS los productos (Sin esconder los que están en cero)
    base_query = Producto.objects.all().order_by('nombre')

    # 2. LA MAGIA MATEMÁTICA (Coalesce): Sumamos stock pero convirtiendo Nulls en 0
    if sucursal_usuario:
        base_query = base_query.annotate(
            stock_total_tmp=Coalesce(Sum('lotes__cantidad', filter=Q(lotes__sucursal=sucursal_usuario)), 0)
        )
    else:
        base_query = base_query.annotate(
            stock_total_tmp=Coalesce(Sum('lotes__cantidad'), 0)
        )

    # 3. Aplicar Búsqueda por texto/código
    if query:
        base_query = base_query.filter(Q(nombre__icontains=query) | Q(codigo_barras__icontains=query))

    # 4. FILTRO DE ALERTAS (Ahora sí detecta los 0 porque no están escondidos)
    if filtro == 'alertas':
        if sucursal_usuario:
            base_query = base_query.filter(
                Q(stock_total_tmp__lt=F('stock_minimo')) | 
                Q(lotes__fecha_vencimiento__lte=hoy + timedelta(days=20), lotes__sucursal=sucursal_usuario, lotes__cantidad__gt=0)
            ).distinct()
        else:
            base_query = base_query.filter(
                Q(stock_total_tmp__lt=F('stock_minimo')) | 
                Q(lotes__fecha_vencimiento__lte=hoy + timedelta(days=20), lotes__cantidad__gt=0)
            ).distinct()

    # 5. Paginación (Cortamos de a 50)
    paginator = Paginator(base_query, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # 6. Optimizamos la velocidad de venta en RAM
    productos_ids = [p.id for p in page_obj.object_list]
    ventas_query = DetalleVenta.objects.filter(venta__fecha_hora__gte=hace_30_dias, producto_id__in=productos_ids)
    
    if sucursal_usuario:
        ventas_query = ventas_query.filter(venta__sucursal=sucursal_usuario)
        
    ventas_dict = ventas_query.values('producto_id').annotate(total=Sum('cantidad'))
    ventas_map = {item['producto_id']: item['total'] for item in ventas_dict}

    info_consolidada = []
    
    # 7. Separación final para la tabla (Góndola vs Depósito)
    for producto in page_obj.object_list:
        lotes = Stock.objects.filter(producto=producto)
        if sucursal_usuario:
            lotes = lotes.filter(sucursal=sucursal_usuario)

        total_gondola = lotes.filter(ubicacion='gondola').aggregate(tot=Sum('cantidad'))['tot'] or 0
        total_deposito = lotes.exclude(ubicacion='gondola').aggregate(tot=Sum('cantidad'))['tot'] or 0
        
        lote_proximo = lotes.filter(cantidad__gt=0).order_by(F('fecha_vencimiento').asc(nulls_last=True)).first()
        vencimiento_proximo = lote_proximo.fecha_vencimiento if lote_proximo else None
        dias_para_vencer = (vencimiento_proximo - hoy).days if vencimiento_proximo else None
        
        ventas_30_dias = ventas_map.get(producto.id, 0)
        velocidad_venta = ventas_30_dias / 30.0 if ventas_30_dias > 0 else 0 

        stock_total = total_gondola + total_deposito
        
        # Riesgo: Si está en 0, menor al mínimo o se vence antes de venderse
        en_riesgo = stock_total == 0 or stock_total < producto.stock_minimo or (
            velocidad_venta > 0 and dias_para_vencer is not None and (stock_total / velocidad_venta) > dias_para_vencer
        )

        info_consolidada.append({
            'producto': producto, 
            'total_gondola': total_gondola,
            'total_deposito': total_deposito,
            'vencimiento_proximo': vencimiento_proximo,
            'dias_para_vencer': dias_para_vencer,
            'en_riesgo': en_riesgo
        })

    page_obj.info_procesada = info_consolidada

    return render(request, 'core/stock_detalle.html', {
        'page_obj': page_obj, 
        'sucursal_actual': sucursal_usuario,
        'query': query
    })
def importar_stock(request):
    query = request.GET.get('q')
    productos = []
    
    if query:
        # BUSQUEDA INTELIGENTE:
        # Busca si el texto está en el Código de Barras O (OR) en el Nombre
        productos = Producto.objects.filter(
            Q(codigo_barras__icontains=query) | 
            Q(nombre__icontains=query)
        )

    return render(request, 'core/importar_stock.html', {'productos': productos})
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

    # --- LA MAGIA NUEVA: EL BUSCADOR ---
    productos = None # Por defecto no mostramos nada
    query = request.GET.get('q') # Atrapamos lo que escribió o escaneó el usuario

    if query:
        # Buscamos si el texto coincide con el nombre o con el código de barras
        productos = Producto.objects.filter(
            Q(nombre__icontains=query) | Q(codigo_barras__icontains=query)
        )
        
        # IMPORTANTE: Si en tu modelo 'Producto' tenés un campo que los separa por negocio/sucursal,
        # descomentá la línea de abajo para que un kiosco no vea los productos del otro:
        # productos = productos.filter(sucursal=sucursal_usuario)

    # Armamos el paquete de datos y se lo mandamos al HTML
    context = {
        'productos': productos,
    }
    
    return render(request, 'core/agregar_stock.html', context)

@login_required
def editar_producto(request, producto_id):
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------
    
    producto = get_object_or_404(Producto, id=producto_id)
    
    if request.method == 'POST':
        # Acá está la magia: le pasamos los datos del formulario Y la 'instance' (el producto a pisar)
        form = ProductoForm(request.POST, request.FILES, instance=producto)
        if form.is_valid():
            form.save()
            messages.success(request, '¡Producto actualizado exitosamente!')
            return redirect('listar_productos')
        else:
            messages.error(request, 'Por favor, revisá los errores del formulario.')
    else:
        # Si recién entra a la página, cargamos el form con los datos actuales del producto
        form = ProductoForm(instance=producto)

    proveedores = Proveedor.objects.all().order_by('nombre')
    categorias = Categoria.objects.all().order_by('nombre')
    categorias_json = json.dumps(
        {cat.id: float(cat.margen_ganancia_porcentaje) for cat in categorias}
    )
    
    # IMPORTANTE: Ahora sí le mandamos el 'form' al HTML
    return render(request, 'core/form_producto.html', {
        'form': form, 
        'producto': producto, 
        'proveedores': proveedores, 
        'categorias': categorias,
        'categorias_json': categorias_json
    })
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

    # Ahora, incluso para el Superadmin, si hay una sucursal seleccionada, filtramos.
    lotes_query = Stock.objects.filter(producto=producto, cantidad__gt=0)
    
    if sucursal_usuario:
        lotes_query = lotes_query.filter(sucursal=sucursal_usuario)
    elif not request.user.is_superuser:
        lotes_query = Stock.objects.none()

    lotes = lotes_query.order_by('fecha_vencimiento')
    return render(request, 'core/detalle_producto_lotes.html', {
        'producto': producto, 
        'lotes': lotes, 
        'sucursal_actual': sucursal_usuario
    })

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
                
                # 1. Calculamos los subtotales base
                subtotal_productos = sum(Decimal(str(item['precio'])) * int(item['cantidad']) for item in carrito if item.get('tipo') != 'devolucion')
                total_devoluciones = sum(Decimal(str(item['precio'])) * int(item['cantidad']) for item in carrito if item.get('tipo') == 'devolucion')
                subtotal_venta = subtotal_productos + total_devoluciones

                # CÁLCULO DE RECARGOS SEGÚN TU LÓGICA FINAL
                descuento_recargo = Decimal('0.00')
                
                for item in carrito:
                    if item.get('tipo') != 'devolucion':
                        producto = get_object_or_404(Producto, id=item['id'])
                        item_subtotal = Decimal(str(item['precio'])) * int(item['cantidad'])
                        
                        if metodo_pago == 'efectivo':
                            descuento_recargo -= item_subtotal * (config.descuento_efectivo_porcentaje / Decimal('100'))
                        
                        elif metodo_pago == 'credito':
                            # CRÉDITO: El local manda para todo el ticket
                            descuento_recargo += item_subtotal * (config.recargo_credito_porcentaje / Decimal('100'))
                        
                        elif metodo_pago == 'debito':
                            # DÉBITO: Individual si es rebelde, sino el del local
                            porcentaje_local = getattr(config, 'recargo_debito_porcentaje', Decimal('0.00'))
                            porcentaje = producto.recargo_credito_individual if producto.aplica_recargo_individual else porcentaje_local
                            descuento_recargo += item_subtotal * (porcentaje / Decimal('100'))
                            
                        elif metodo_pago == 'qr':
                            # QR: Individual si es rebelde, sino el del local
                            porcentaje = producto.recargo_qr_individual if producto.aplica_recargo_individual else config.recargo_qr_porcentaje
                            descuento_recargo += item_subtotal * (porcentaje / Decimal('100'))

                total_venta = subtotal_venta + descuento_recargo
                total_venta_quantized = total_venta.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

                # CORRECCIÓN: Usamos 'cuenta_corriente' en lugar de 'saldo_actual'
                # Chequeo de límite de crédito (usamos un valor alto por defecto si no existe el campo límite)
                limite = getattr(cliente, 'limite_credito', 999999)
                if cliente and (cliente.cuenta_corriente + total_venta_quantized) > limite:
                    raise Exception(f'Límite de crédito excedido. Deuda actual: ${cliente.cuenta_corriente}. Límite: ${limite}.')

                nueva_venta = Venta.objects.create(
                    subtotal=subtotal_venta, 
                    descuento_recargo=descuento_recargo.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    total=total_venta_quantized, 
                    metodo_pago=metodo_pago, 
                    cuotas=cuotas,
                    sucursal=sucursal_usuario,
                    cliente=cliente 
                )

                # Si fue fiado, actualizamos la cuenta corriente del cliente
                if cliente:
                    cliente.cuenta_corriente += total_venta_quantized
                    cliente.save()

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
                        
                        # ARREGLO DE VENTAS: Quitamos "ubicacion='gondola'" para que venda de donde haya stock.
                        # El '-ubicacion' asegura que consuma primero de la 'gondola' y luego del resto.
                        lotes_disponibles = Stock.objects.filter(
                            producto=producto, cantidad__gt=0, sucursal=sucursal_usuario
                        ).order_by('-ubicacion', 'fecha_vencimiento')
                        
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

    # 1. OPTIMIZACIÓN: Ya tenías prefetch_related, lo mantenemos porque es excelente
    ventas_list = ventas_query.order_by('-fecha_hora').prefetch_related('detalles__producto')
    
    # 2. PAGINACIÓN: Cortamos la lista de a 50 ventas por página
    paginator = Paginator(ventas_list, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Mandamos 'page_obj' al HTML en lugar de 'ventas'
    return render(request, 'core/historial_ventas.html', {
        'page_obj': page_obj, 
        'sucursal_actual': sucursal_usuario
    })
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
    
    # OPTIMIZACIÓN: Buscamos por nombre O por código en la misma caja
    productos = Producto.objects.filter(
        Q(nombre__icontains=query) | Q(codigo_barras__icontains=query)
    )[:10]
    
    resultados = [{
        'id': p.id, 
        'nombre': p.nombre, 
        'precio': float(p.precio_venta), # Convertimos a float para que el JSON no falle
        'aplica_recargo_individual': p.aplica_recargo_individual,
        'recargo_credito_individual': float(p.recargo_credito_individual),
        'recargo_qr_individual': float(p.recargo_qr_individual)
    } for p in productos]
    
    return JsonResponse(resultados, safe=False)

@login_required
def buscar_producto_por_codigo(request):
    codigo = request.GET.get('codigo', '')
    try:
        # Buscamos el producto
        producto = Producto.objects.get(codigo_barras=codigo)
        
        # CORRECCIÓN: Cambié 'p' por 'producto' para evitar el error de NameError
        resultado = {
            'id': producto.id, 
            'nombre': producto.nombre, 
            'precio': float(producto.precio_venta),
            'aplica_recargo_individual': producto.aplica_recargo_individual,
            'recargo_credito_individual': float(producto.recargo_credito_individual),
            'recargo_qr_individual': float(producto.recargo_qr_individual)
        }
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

# =========================================================
# CONFIGURACIÓN DE GEMINI API (El "Cerebro")
# =========================================================
genai.configure(api_key='AIzaSyAP_WUbRpfdkPG3LTG08JkljlxeDUwFWls')

@login_required
def cargar_factura_ocr(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    if not sucursal_usuario and request.user.perfilusuario.rol != 'admin':
        messages.error(request, "Necesitas una sucursal asignada.")
        return redirect('dashboard')

    if request.method == 'POST' and request.FILES.get('imagen_factura'):
        try:
            # =========================================================
            # OPTIMIZACIÓN 1: DIETA DE IMAGEN (BLANCO Y NEGRO + RESIZE)
            # =========================================================
            archivo_imagen = request.FILES['imagen_factura']
            img = Image.open(archivo_imagen)
            
            # Pasamos a escala de grises (L) para eliminar ruido de color
            img = img.convert('L')
            # Aumentamos un poquito el contraste para que las letras resalten
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(1.5)
            # Reducimos a 800x800 (Suficiente para leer texto, gasta mínimos tokens)
            img.thumbnail((800, 800))

            # Usamos Flash 1.5 que es el más estable y económico
            modelo_ia = genai.GenerativeModel('gemini-2.5-flash-lite')
            
            # =========================================================
            # OPTIMIZACIÓN 2: MICRO-PROMPT (Cero palabras innecesarias)
            # =========================================================
            instruccion = """Extrae datos de la factura. Ignora CUITs, teléfonos y totales.
            Devuelve SOLO un JSON con esta estructura exacta:
            {"proveedor": "Nombre", "productos": [{"cantidad": 1, "descripcion": "ejemplo", "precio_unitario": 100.5}]}"""
            
            # =========================================================
            # OPTIMIZACIÓN 3: MODO JSON NATIVO
            # =========================================================
            # Esto obliga a la IA a no escribir texto, SOLO código JSON. Ahorra tokens de salida.
            config_json = genai.GenerationConfig(response_mime_type="application/json")

            # =========================================================
            # LÓGICA DE REINTENTOS (Tolerancia a fallos de cuota)
            # =========================================================
            max_intentos = 3
            datos_ordenados = {}
            
            for intento in range(max_intentos):
                try:
                    respuesta_ia = modelo_ia.generate_content(
                        [instruccion, img], 
                        generation_config=config_json # Aplicamos la regla estricta
                    )
                    # Como ya viene en JSON puro, no hace falta hacer .replace("```json")
                    datos_ordenados = json.loads(respuesta_ia.text)
                    break 
                except Exception as error_ia:
                    if '429' in str(error_ia) and intento < max_intentos - 1:
                        time.sleep(4) # Pausa de 4 segs si hay cuello de botella
                        continue
                    else:
                        raise error_ia

            # =========================================================
            # PROCESAMIENTO EN PYTHON (La IA ya no trabaja acá, trabaja tu CPU)
            # =========================================================
            proveedor_detectado = datos_ordenados.get("proveedor", "Proveedor Desconocido")
            lista_productos_ia = datos_ordenados.get("productos", [])

            productos_encontrados = []
            mi_negocio = request.user.perfilusuario.negocio
            todos_los_productos = Producto.objects.filter(negocio=mi_negocio)
            
            for index, item in enumerate(lista_productos_ia, start=1):
                descripcion = item.get('descripcion', 'Sin descripción')
                cantidad = item.get('cantidad', 1)
                
                # Filtro de seguridad rápido en Python
                if type(cantidad) == str or cantidad > 5000: cantidad = 1
                
                costo = item.get('precio_unitario', 0)
                
                producto_db_match = None
                for p_db in todos_los_productos:
                    if p_db.nombre.lower() in descripcion.lower():
                        producto_db_match = p_db
                        break

                productos_encontrados.append({
                    'id_temporal': index,
                    'descripcion_factura': descripcion,
                    'cantidad_sugerida': cantidad,
                    'costo_sugerido': str(costo).replace(',', '.'),
                    'precio_venta_sugerido': '0',
                    'producto_db': producto_db_match,
                })

            return render(request, 'core/confirmar_factura_ocr.html', {
                'productos_encontrados': productos_encontrados,
                'texto_completo_ocr': "Lectura Visual Optimizada (Escala de grises, contraste mejorado, JSON nativo).",
                'proveedor_detectado': proveedor_detectado,
                'todos_los_productos': todos_los_productos
            })

        except Exception as e:
            messages.error(request, f"Error del sistema OCR: {e}")
            return redirect('cargar_factura_ocr')

    return render(request, 'core/cargar_factura_ocr.html')
@login_required
def guardar_factura_confirmada(request):
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    # Validación de seguridad SaaS
    if not sucursal_usuario and request.user.perfilusuario.rol != 'admin':
        messages.error(request, "Error: No se pudo determinar tu sucursal.")
        return redirect('dashboard')

    if request.method == 'POST':
        ubicacion = request.POST.get('ubicacion', 'deposito')
        items = {}
        
        # 1. Agrupamos todos los datos del formulario por el ID de la fila
        for key, value in request.POST.items():
            if '_' in key and not key.startswith('csrf'):
                try:
                    parts = key.split('_')
                    item_id = parts[-1]
                    field_name = '_'.join(parts[:-1])
                    if item_id not in items: items[item_id] = {}
                    items[item_id][field_name] = value
                except ValueError:
                    pass

        items_cargados = 0
        try:
            with transaction.atomic():
                for item_id, data in items.items():
                    # Validación básica: si falta algo, salteamos la fila
                    if not data.get('producto') or not data.get('cantidad') or not data.get('costo'):
                        continue
                    
                    try:
                        producto_id = data['producto']
                        cantidad = int(data['cantidad'])
                        costo = Decimal(data['costo'])
                        precio_venta_str = data.get('precio_venta', '0')
                        precio_venta = Decimal(precio_venta_str) if precio_venta_str else Decimal('0')
                        
                        # --- ACÁ ESTÁ LA MAGIA DE LA FECHA INDIVIDUAL ---
                        fecha_venc_str = data.get('fecha_vencimiento')
                        fecha_vencimiento_item = fecha_venc_str if fecha_venc_str else None

                    except (ValueError, TypeError):
                        messages.warning(request, f"Datos inválidos en una de las filas. Omitida.")
                        continue

                    if cantidad <= 0 or costo < 0: continue

                    # 2. Actualizamos el costo y precio del Producto
                    producto = get_object_or_404(Producto, id=producto_id)
                    producto.costo = costo
                    
                    if precio_venta > 0: 
                        producto.precio_venta = precio_venta
                    elif producto.categoria and producto.categoria.margen_ganancia_porcentaje > 0:
                        margen = producto.categoria.margen_ganancia_porcentaje / Decimal(100)
                        producto.precio_venta = (costo * (1 + margen)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    producto.save()

                    # 3. Guardamos el Lote de Stock con SU propia fecha
                    Stock.objects.create(
                        producto=producto, 
                        cantidad=cantidad, 
                        fecha_vencimiento=fecha_vencimiento_item, 
                        ubicacion=ubicacion, 
                        sucursal=sucursal_usuario
                    )
                    items_cargados += 1

            # Mensajes de éxito
            if items_cargados > 0: 
                messages.success(request, f"¡Factura cargada! Se añadieron {items_cargados} productos al stock.")
            else: 
                messages.warning(request, "No se cargaron items válidos.")
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
# --- VALIDACIÓN DE PERMISOS ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Solo los administradores pueden cargar nuevos proveedores.")
        return redirect('listar_proveedores')
    # ------------------------------
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
# --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------
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
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

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
    # --- ESCUDO DE SEGURIDAD (Opcional, si querés que solo el admin vea esta lista) ---
    # es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    # if not request.user.is_superuser and not es_admin_local:
    #     messages.error(request, "Acceso denegado.")
    #     return redirect('dashboard')
    # ----------------------------------------------------------------

    query = request.GET.get('q', '')
    
    # 1. OPTIMIZACIÓN EXTREMA: Hacemos 1 solo viaje a la BD trayendo proveedor y categoría
    productos_list = Producto.objects.select_related('proveedor', 'categoria').all()

    # 2. BUSCADOR EN EL SERVIDOR
    if query:
        productos_list = productos_list.filter(
            Q(nombre__icontains=query) | 
            Q(codigo_barras__icontains=query)
        )
    
    productos_list = productos_list.order_by('nombre')

    # 3. PAGINACIÓN: Cortamos la torta de a 50 porciones
    paginator = Paginator(productos_list, 50) 
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Mandamos 'page_obj' a la plantilla
    return render(request, 'core/listar_productos.html', {
        'page_obj': page_obj,
        'query': query # Para mantener escrito lo que buscó
    })
def crear_producto(request):
    if request.method == 'POST':
        # Cargamos los datos que mandó el usuario en el formulario
        form = ProductoForm(request.POST) 
        
        if form.is_valid():
            try:
                form.save() # ¡Se guarda solo! Magia de Django
                messages.success(request, '¡Producto creado exitosamente!')
                return redirect('listar_productos') # O el nombre de tu url de lista
            except Exception as e:
                messages.error(request, f'Error al guardar: {e}')
        else:
            messages.error(request, 'Por favor corrige los errores en el formulario.')
    else:
        # Si es GET (cuando entrás a la página), creamos un formulario vacío
        form = ProductoForm()

    # Datos extra para los selectores o JS (opcional si usás el form automático)
    proveedores = Proveedor.objects.all().order_by('nombre')
    categorias = Categoria.objects.all().order_by('nombre')
    
    # Esto es para tu JS de margen de ganancia
    categorias_json = json.dumps(
        {cat.id: float(cat.margen_ganancia_porcentaje) for cat in categorias}
    )

    context = {
        'form': form, # <--- ESTO ES LO QUE FALTABA PARA QUE APAREZCAN LOS CAMPOS
        'proveedores': proveedores,
        'categorias': categorias,
        'categorias_json': categorias_json,
    }
    return render(request, 'core/form_producto.html', context)


@login_required
def eliminar_producto(request, producto_id):
    # --- SOLO SUPERUSUARIO ---
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

    
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
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

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
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

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
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

    categoria = get_object_or_404(Categoria, id=categoria_id)
    if request.method == 'POST':
        categoria.delete()
        messages.success(request, '¡Categoría eliminada!')
    return redirect('listar_categorias')

#==============================================================================
# GESTIÓN DE CLIENTES Y CUENTA CORRIENTE (CORREGIDO)
# ==============================================================================

@login_required
def listar_clientes(request):
    query = request.GET.get('q')
    filtro_deuda = request.GET.get('filtro') # <--- NUEVO: Detecta si pedimos solo deudores
    
    # Base: Calculamos el valor absoluto del saldo
    clientes = Cliente.objects.annotate(
        saldo_abs=Abs('cuenta_corriente')
    ).order_by('nombre')
    
    # 1. Si apretaste "Cuentas Corrientes", filtramos solo los que tienen deuda positiva
    if filtro_deuda == 'deudores':
        clientes = clientes.filter(cuenta_corriente__gt=0) # Solo los que deben (> 0)

    # 2. Si usaste el buscador
    if query:
        clientes = clientes.filter(
            Q(nombre__icontains=query) | 
            Q(dni__icontains=query)
        )

    return render(request, 'core/listar_clientes.html', {
        'clientes': clientes,
        'solo_deudores': (filtro_deuda == 'deudores') # Para mostrar un título distinto en el HTML si querés
    })
@login_required
def crear_cliente(request):
    if request.method == 'POST':
        # Atrapamos el saldo que el cliente ya debe. Si está vacío, es 0.
        saldo_inicial_str = request.POST.get('saldo_inicial', '0')
        try:
            saldo_inicial = Decimal(saldo_inicial_str) if saldo_inicial_str else Decimal('0.00')
        except (ValueError, TypeError):
            saldo_inicial = Decimal('0.00')

        try:
            Cliente.objects.create(
                nombre=request.POST['nombre'],
                dni=request.POST.get('dni'),
                telefono=request.POST.get('telefono'),
                direccion=request.POST.get('direccion'),
                cuenta_corriente=saldo_inicial # <-- Guardamos la deuda previa
            )
            messages.success(request, '¡Cliente registrado con éxito!')
            return redirect('listar_clientes')
        except Exception as e:
            messages.error(request, f'Error al crear: {e}')

    return render(request, 'core/form_cliente.html', {'titulo': 'Nuevo Cliente'})
@login_required
def editar_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, id=cliente_id)

    if request.method == 'POST':
        cliente.nombre = request.POST['nombre']
        cliente.dni = request.POST.get('dni')
        cliente.telefono = request.POST.get('telefono')
        cliente.direccion = request.POST.get('direccion')
        cliente.save()
        messages.success(request, 'Datos actualizados.')
        return redirect('listar_clientes')

    return render(request, 'core/form_cliente.html', {'cliente': cliente, 'titulo': 'Editar Cliente'})

@login_required
def estado_cuenta_cliente(request, cliente_id):
    cliente = get_object_or_404(Cliente, id=cliente_id)

    # 1. Ventas Fiadas (Deuda)
    ventas_fiadas = Venta.objects.filter(
        cliente=cliente, 
        metodo_pago='cuenta_corriente'
    ).order_by('-fecha_hora')

    # 2. Pagos Realizados (Haber)
    pagos_realizados = PagoCliente.objects.filter(cliente=cliente).order_by('-fecha')

    context = {
        'cliente': cliente,
        'ventas_fiadas': ventas_fiadas,
        'pagos_realizados': pagos_realizados,
    }
    return render(request, 'core/estado_cuenta_cliente.html', context)

@login_required
def registrar_pago_cliente(request, cliente_id):
    if request.method != 'POST':
        return redirect('listar_clientes')

    sucursal_usuario = obtener_sucursal_usuario(request)
    cliente = get_object_or_404(Cliente, id=cliente_id)
    monto_str = request.POST.get('monto')

    try:
        monto = Decimal(monto_str)
        if monto <= 0: raise ValueError("Monto inválido")

        with transaction.atomic():
            # A. Guardar el Pago en el Historial
            PagoCliente.objects.create(
                cliente=cliente,
                sucursal=sucursal_usuario, # Puede ser None si es admin global, ojo con esto en el modelo
                monto=monto
            )
            
            # B. Bajar la Deuda (Cuenta Corriente)
            # Si cuenta_corriente es POSITIVA significa que DEBE plata.
            # Al pagar, restamos.
            cliente.cuenta_corriente -= monto
            cliente.save()

        messages.success(request, f"Pago de ${monto} registrado exitosamente.")

    except Exception as e:
        messages.error(request, f"Error al registrar pago: {e}")

    return redirect('estado_cuenta_cliente', cliente_id=cliente.id)


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
    mi_negocio = request.user.perfilusuario.negocio
    # ¡CLAVE! Buscar solo las ventas de las sucursales de ESTE negocio
    ventas_del_negocio = Venta.objects.filter(sucursal__negocio=mi_negocio)
    # ... resto de tu algoritmo de reglas de asociación ...
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    # 1. Filtramos: Solo ventas de la sucursal actual y del último mes (para rendimiento)
    fecha_limite = timezone.now() - timedelta(days=30)
    
    ventas_query = Venta.objects.filter(fecha_hora__gte=fecha_limite).prefetch_related('detalles__producto')
    
    if sucursal_usuario:
        ventas_query = ventas_query.filter(sucursal=sucursal_usuario)

    # 2. Convertir a transacciones
    transacciones = []
    for venta in ventas_query:
        if venta.detalles.count() > 1:
            # Usamos una lista de strings con los nombres de productos
            productos = [d.producto.nombre for d in venta.detalles.all()]
            transacciones.append(productos)

    resultados_apyori = []
    
    # Solo ejecutamos si hay suficientes datos
    if len(transacciones) > 5:
        try:
            reglas = apriori(transacciones, min_support=0.02, min_confidence=0.1, min_lift=1.1, min_length=2)
            
            for regla in reglas:
                for item_set in regla.ordered_statistics:
                    if len(item_set.items_base) == 1 and len(item_set.items_add) == 1:
                        resultados_apyori.append({
                            'base': list(item_set.items_base)[0],
                            'add': list(item_set.items_add)[0],
                            'soporte': round(regla.support * 100, 1),
                            'confianza': round(item_set.confidence * 100, 1),
                            'lift': round(item_set.lift, 2)
                        })
                        
            resultados_apyori.sort(key=lambda x: x['confianza'], reverse=True)
        except Exception as e:
            messages.warning(request, f"No hay suficientes patrones de venta aún.")
            
    return render(request, 'core/analisis_canasta.html', {
        'reglas': resultados_apyori, 
        'total_transacciones': len(transacciones)
    })

@login_required
def sugerencias_compra(request):
    mi_negocio = request.user.perfilusuario.negocio
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    if not sucursal_usuario and not request.user.perfilusuario.rol == 'admin':
        messages.error(request, "Necesitas una sucursal asignada para ver esto.")
        return redirect('dashboard')

    hoy = timezone.now().date()
    
    # 1. Filtramos solo los productos de ESTE negocio
    productos = Producto.objects.filter(negocio=mi_negocio).select_related('proveedor')

    sugerencias_por_proveedor = {}

    for producto in productos:
        # --- A. Calcular Stock Actual ---
        if sucursal_usuario:
            stock_actual = Stock.objects.filter(producto=producto, sucursal=sucursal_usuario).aggregate(Sum('cantidad'))['cantidad__sum'] or 0
        else:
            stock_actual = Stock.objects.filter(producto=producto).aggregate(Sum('cantidad'))['cantidad__sum'] or 0

        # --- B. Determinar Fecha del Próximo Pedido ---
        proveedor = producto.proveedor
        fecha_entrega = None
        
        if proveedor:
            nombre_proveedor = proveedor.nombre
            fecha_entrega = proveedor.proxima_fecha_entrega()
        else:
            nombre_proveedor = "Sin Proveedor Asignado"

        # Si no hay fecha de entrega configurada, asumimos que pedimos para cubrir 7 días
        if not fecha_entrega:
            fecha_entrega = hoy + timedelta(days=7)

        # --- C. Consultar a la IA (Predicciones) ---
        # Sumamos cuánto dice la IA que vamos a vender desde HOY hasta que llegue el camión
        predicciones = PrediccionVenta.objects.filter(
            producto=producto,
            fecha__gte=hoy,
            fecha__lte=fecha_entrega
        )
        
        if sucursal_usuario:
            predicciones = predicciones.filter(sucursal=sucursal_usuario)

        ventas_esperadas = predicciones.aggregate(Sum('cantidad_predicha'))['cantidad_predicha__sum'] or 0
        ventas_esperadas = float(ventas_esperadas)

        # --- D. La Matemática del Kiosco (Stock Proyectado) ---
        stock_proyectado = float(stock_actual) - ventas_esperadas

        # --- E. Decisión de Compra ---
        # Si de acá a que llegue el camión nos quedamos por debajo del mínimo... ¡A PEDIR!
        if stock_proyectado < float(producto.stock_minimo):
            
            # ¿Cuánto pedimos? Lo que vamos a vender + lo que nos falta para cubrir el mínimo
            cantidad_a_pedir = float(producto.stock_minimo) - stock_proyectado
            cantidad_a_pedir = int(round(cantidad_a_pedir)) # Redondeamos a números enteros

            if cantidad_a_pedir > 0:
                if nombre_proveedor not in sugerencias_por_proveedor:
                    sugerencias_por_proveedor[nombre_proveedor] = []

                sugerencias_por_proveedor[nombre_proveedor].append({
                    'nombre': producto.nombre,
                    'stock_actual': stock_actual,
                    'ventas_esperadas': round(ventas_esperadas, 1),
                    'fecha_entrega': fecha_entrega.strftime('%d/%m/%Y'),
                    'stock_proyectado': round(stock_proyectado, 1),
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
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------
    # 1. Obtener parámetros de filtro (GET)
    fecha_inicio_str = request.GET.get('fecha_inicio')
    fecha_fin_str = request.GET.get('fecha_fin')
    sucursal_id = request.GET.get('sucursal_id')

    # 2. Configurar Fechas (Por defecto: Mes actual)
    hoy = timezone.now()
    if fecha_inicio_str:
        fecha_inicio = parse_date(fecha_inicio_str)
    else:
        fecha_inicio = hoy.date().replace(day=1) # Primer día del mes

    if fecha_fin_str:
        fecha_fin = parse_date(fecha_fin_str)
    else:
        fecha_fin = hoy.date() # Hoy

    # Ajuste técnico: Para incluir todo el día final, sumamos 1 día al cierre
    # (Porque la venta es '2024-02-10 15:30' y el filtro '2024-02-10' corta a las 00:00)
    fecha_fin_filtro = fecha_fin + timezone.timedelta(days=1)

    # 3. Base Query
    ventas = Venta.objects.filter(fecha_hora__range=[fecha_inicio, fecha_fin_filtro])

    # 4. Filtro de Sucursal
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    # Si es admin y eligió una sucursal específica en el filtro
    if request.user.is_superuser and sucursal_id:
        ventas = ventas.filter(sucursal_id=sucursal_id)
        sucursal_actual = Sucursal.objects.get(id=sucursal_id)
    # Si es admin y no eligió (ve todo)
    elif request.user.is_superuser:
        sucursal_actual = None # "Todas"
    # Si es empleado, solo ve su sucursal
    elif sucursal_usuario:
        ventas = ventas.filter(sucursal=sucursal_usuario)
        sucursal_actual = sucursal_usuario
    else:
        ventas = Venta.objects.none() # Seguridad
        sucursal_actual = None

    # 5. Cálculos para Gráficos y Tarjetas
    
    # A. Ingresos por Método de Pago
    metodos = ['efectivo', 'debito', 'credito', 'qr', 'cuenta_corriente']
    datos_metodos = []
    labels_metodos = []
    colors_metodos = ['#28a745', '#007bff', '#dc3545', '#17a2b8', '#ffc107'] # Colores fijos
    
    total_ingresos_reales = 0
    total_fiado = 0

    for metodo in metodos:
        total = ventas.filter(metodo_pago=metodo).aggregate(Sum('total'))['total__sum'] or 0
        total = float(total) # Convertir Decimal a float para JSON
        
        datos_metodos.append(total)
        labels_metodos.append(metodo.capitalize().replace('_', ' '))
        
        if metodo == 'cuenta_corriente':
            total_fiado += total
        else:
            total_ingresos_reales += total

    # B. Ventas vs Costos (Estimado simple)
    # Si tenés costos en DetalleVenta, podrías sumar aquí. Por ahora usaremos Total Venta.
    
    # 6. Contexto para el Template
    context = {
        'fecha_inicio': fecha_inicio.strftime('%Y-%m-%d'),
        'fecha_fin': fecha_fin.strftime('%Y-%m-%d'),
        'sucursal_seleccionada': sucursal_actual,
        'todas_las_sucursales': Sucursal.objects.all() if request.user.is_superuser else None,
        
        # Totales Tarjetas
        'total_general': total_ingresos_reales + total_fiado,
        'total_caja_real': total_ingresos_reales,
        'total_fiado': total_fiado,
        'cantidad_ventas': ventas.count(),
        
        # Datos para Gráficos (JSON)
        'chart_labels': json.dumps(labels_metodos),
        'chart_data': json.dumps(datos_metodos),
        'chart_colors': json.dumps(colors_metodos),
        
        # Listado Detallado (Últimas 100)
        'ventas_listado': ventas.order_by('-fecha_hora')[:100]
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

def historial_cierres_caja(request):
    # Detectamos qué sucursal debe ver el usuario
    sucursal_usuario = obtener_sucursal_usuario(request)
    
    # Traemos los cierres del más nuevo al más viejo
    cierres_query = CierreTurno.objects.all().order_by('-fecha_cierre_turno')
    
    # Filtramos por sucursal si no es el administrador global
    if not request.user.is_superuser and sucursal_usuario:
        cierres_query = cierres_query.filter(sucursal=sucursal_usuario)
    elif not request.user.is_superuser:
        cierres_query = CierreTurno.objects.none()
        messages.warning(request, "No tenés permiso para ver cierres globales.")

    # Paginamos de a 20 cierres por página
    paginator = Paginator(cierres_query, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'core/historial_cierres.html', {
        'page_obj': page_obj,
        'sucursal_actual': sucursal_usuario
    })

@login_required
def cambiar_sucursal_sesion(request, sucursal_id):
    es_admin = request.user.is_superuser or (hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin')
    
    if not es_admin:
        messages.error(request, "Solo el administrador puede cambiar de sucursal.")
        return redirect('dashboard')
        
    try:
        sucursal = get_object_or_404(Sucursal, id=sucursal_id)
        request.session['sucursal_seleccionada_id'] = sucursal.id
        messages.success(request, f"Ahora estás administrando: {sucursal.nombre}")
    except Exception as e:
        messages.error(request, "Error al cambiar de sucursal.")
    
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

# ==============================================================================
# VISTA DE ESCANEO CON CELULAR
# ==============================================================================

# 1. PC: Crea la sesión al abrir la caja
def iniciar_sesion_escaneo(request):
    sesion = SesionEscaneo.objects.create()
    return JsonResponse({'uuid': str(sesion.uuid)})

# 2. PC: Pregunta "¿Hay algo nuevo?" (Polling)
def check_scan(request, uuid):
    try:
        sesion = SesionEscaneo.objects.get(uuid=uuid)
        # Si hay un código nuevo que no fue procesado
        if not sesion.codigo_procesado and sesion.ultimo_codigo:
            codigo = sesion.ultimo_codigo
            sesion.codigo_procesado = True
            sesion.save()
            
            # --- CORRECCIÓN CLAVE ---
            # Devolvemos SIEMPRE el código crudo
            respuesta = {
                'nuevo': True, 
                'codigo': codigo, # <--- ESTO FALTABA
                'producto': None 
            }

            # Opcional: Si existe en TU base de datos, mandamos datos extra
            try:
                producto = Producto.objects.get(codigo_barras=codigo)
                respuesta['producto'] = {
                    'id': producto.id, 
                    'nombre': producto.nombre, 
                    'precio': str(producto.precio_venta), 
                    'codigo': codigo
                }
            except Producto.DoesNotExist:
                pass # No pasa nada, es un producto nuevo
                
            return JsonResponse(respuesta)
                
        return JsonResponse({'nuevo': False})
    except SesionEscaneo.DoesNotExist:
        return JsonResponse({'error': 'Sesión inválida'}, status=404)

# 3. CELULAR: Envía el código que leyó la cámara
@csrf_exempt
def enviar_codigo_remoto(request, uuid):
    if request.method == 'POST':
        data = json.loads(request.body)
        codigo = data.get('codigo')
        try:
            sesion = SesionEscaneo.objects.get(uuid=uuid)
            sesion.ultimo_codigo = codigo
            sesion.codigo_procesado = False # Avisamos que hay algo nuevo sin leer
            sesion.save()
            return JsonResponse({'success': True})
        except SesionEscaneo.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Sesión no existe'})
    return JsonResponse({'success': False})

# 4. CELULAR: La página que ve el empleado en el teléfono
def pantalla_scanner_remoto(request, uuid):
    return render(request, 'core/scanner_remoto.html', {'uuid': uuid})

@login_required
def configuracion(request):
    config = Configuracion.objects.first()
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------
    if request.method == 'POST':
        # Si no existe configuración, la creamos en memoria para llenarla
        if not config:
            config = Configuracion()

        # 1. Datos Generales
        config.nombre_negocio = request.POST.get('nombre_negocio')
        
        # 2. Datos Financieros (Convertimos texto a Decimal)
        try:
            config.descuento_efectivo_porcentaje = Decimal(request.POST.get('descuento_efectivo', 0))
            config.recargo_credito_porcentaje = Decimal(request.POST.get('recargo_credito', 0))
            config.recargo_qr_porcentaje = Decimal(request.POST.get('recargo_qr', 0))
            
            config.save()
            messages.success(request, '¡Configuración guardada exitosamente!')
        except Exception as e:
            messages.error(request, f'Error al guardar valores numéricos: {e}')
        
        return redirect('configuracion')
    
    return render(request, 'core/configuracion.html', {'config': config})
# ==============================================================================
# GESTIÓN DE SUCURSALES (LÓGICA NUEVA)
# ==============================================================================


@login_required
def detalle_categoria(request, categoria_id):
    categoria = get_object_or_404(Categoria, id=categoria_id)
    productos_en_categoria = Producto.objects.filter(categoria=categoria).order_by('nombre')
    
    # Si manda un formulario para sacar o agregar un producto
    if request.method == 'POST':
        accion = request.POST.get('accion')
        producto_id = request.POST.get('producto_id')
        
        if accion == 'quitar':
            prod = get_object_or_404(Producto, id=producto_id)
            prod.categoria = None
            prod.save()
            messages.success(request, f"Se quitó {prod.nombre} de esta categoría.")
            
        elif accion == 'agregar':
            prod = get_object_or_404(Producto, id=producto_id)
            prod.categoria = categoria
            prod.save()
            messages.success(request, f"{prod.nombre} agregado a {categoria.nombre}.")
            
        return redirect('detalle_categoria', categoria_id=categoria.id)
        
    return render(request, 'core/detalle_categoria.html', {
        'categoria': categoria,
        'productos': productos_en_categoria
    })
# --- ARREGLO EN EL CAMBIO DE SUCURSAL ---
@login_required
def seleccionar_sucursal(request, sucursal_id):
    es_admin = request.user.is_superuser or (hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin')
    
    if not es_admin:
        messages.error(request, "Solo el administrador puede cambiar de sucursal.")
        return redirect('dashboard')

    if sucursal_id == 0:
        if 'sucursal_seleccionada_id' in request.session:
            del request.session['sucursal_seleccionada_id']
        messages.info(request, "Viendo datos de: TODAS LAS SUCURSALES")
    else:
        sucursal = get_object_or_404(Sucursal, id=sucursal_id)
        request.session['sucursal_seleccionada_id'] = sucursal.id
        messages.success(request, f"Viendo datos de: {sucursal.nombre}")

    return redirect('dashboard')
# --- NUEVO ESCUDO DE SEGURIDAD PARA ADMINS ---
class AdminNegocioCheck(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        if user.is_superuser:
            return True
        # Si no es superusuario, nos fijamos si su rol es 'admin'
        if hasattr(user, 'perfilusuario') and user.perfilusuario.rol == 'admin':
            return True
        return False

# --- ABM DE SUCURSALES (Ahora usan el nuevo escudo) ---
class SucursalListView(AdminNegocioCheck, ListView):
    model = Sucursal
    template_name = 'core/sucursales_list.html'
    context_object_name = 'lista_sucursales'

class SucursalCreateView(AdminNegocioCheck, CreateView):
    model = Sucursal
    fields = ['nombre', 'direccion']
    template_name = 'core/sucursal_form.html'
    success_url = reverse_lazy('listar_sucursales')
    
    def form_valid(self, form):
        messages.success(self.request, "Sucursal creada correctamente")
        return super().form_valid(form)

class SucursalUpdateView(AdminNegocioCheck, UpdateView):
    model = Sucursal
    fields = ['nombre', 'direccion']
    template_name = 'core/sucursal_form.html'
    success_url = reverse_lazy('listar_sucursales')

# --- NUEVA VISTA PARA ELIMINAR SUCURSALES ---
class SucursalDeleteView(AdminNegocioCheck, DeleteView):
    model = Sucursal
    success_url = reverse_lazy('listar_sucursales')
    
    def form_valid(self, form):
        messages.success(self.request, "Sucursal eliminada correctamente.")
        return super().form_valid(form)

@login_required
def gestionar_usuarios(request):
    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "Acceso denegado. Función exclusiva para administradores.")
        return redirect('dashboard')
    # ---------------------------

    # Tratamos de buscar el negocio del usuario actual
    mi_negocio = None
    if hasattr(request.user, 'perfilusuario'):
        mi_negocio = request.user.perfilusuario.negocio
    elif not request.user.is_superuser:
        messages.error(request, "Tu usuario no está vinculado a ningún negocio.")
        return redirect('dashboard')

    # 2. PROCESAR CREACIÓN DE USUARIO
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        sucursal_id = request.POST.get('sucursal_id')
        rol = request.POST.get('rol')

        try:
            with transaction.atomic():
                # A. Creamos el usuario base de Django
                nuevo_user = User.objects.create_user(username=username, email=email, password=password)
                
                # B. Le creamos su PerfilUsuario atado a NUESTRO negocio
                sucursal_obj = Sucursal.objects.get(id=sucursal_id, negocio=mi_negocio) if sucursal_id else None
                
                PerfilUsuario.objects.create(
                    usuario=nuevo_user,
                    negocio=mi_negocio, # Obligamos a que pertenezca al mismo negocio que el creador
                    sucursal=sucursal_obj,
                    rol=rol
                )
            messages.success(request, f"Usuario {username} creado exitosamente.")
        except Exception as e:
            messages.error(request, f"Error al crear usuario: Ya existe ese nombre de usuario o faltan datos.")
            
        return redirect('gestionar_usuarios')

    # 3. MOSTRAR LA LISTA (Filtrada por Tenant)
    # Solo vemos los usuarios que pertenecen a ESTE negocio
    usuarios_negocio = PerfilUsuario.objects.filter(negocio=mi_negocio).select_related('usuario', 'sucursal')
    mis_sucursales = Sucursal.objects.filter(negocio=mi_negocio)

    context = {
        'usuarios': usuarios_negocio,
        'sucursales': mis_sucursales,
        'negocio': mi_negocio
    }
    return render(request, 'core/gestionar_usuarios.html', context)

@login_required
def sumar_stock_producto(request, producto_id):
    sucursal_usuario = obtener_sucursal_usuario(request)
    if not sucursal_usuario:
        messages.error(request, "Necesitás estar asignado a una sucursal para ingresar mercadería.")
        return redirect('dashboard')

    producto = get_object_or_404(Producto, id=producto_id)

    # Evitamos que modifiquen productos de otra sucursal si el sistema las separa
    if not request.user.is_superuser and producto.lotes.filter(sucursal=sucursal_usuario).exists() is False and producto.lotes.exists():
        pass # Si usas lógica estricta multi-sucursal, podes validarlo acá

    if request.method == 'POST':
        cantidad = int(request.POST.get('cantidad', 0))
        ubicacion = request.POST.get('ubicacion')
        fecha_venc = request.POST.get('fecha_vencimiento')

        if cantidad > 0:
            # Creamos el nuevo lote
            Stock.objects.create(
                producto=producto,
                cantidad=cantidad,
                ubicacion=ubicacion,
                fecha_vencimiento=fecha_venc if fecha_venc else None,
                sucursal=sucursal_usuario
            )
            messages.success(request, f"¡Ingresaste {cantidad} unidades de {producto.nombre} a {ubicacion.capitalize()}!")
            return redirect('agregar_stock') # Lo devolvemos al buscador para el siguiente producto
        else:
            messages.error(request, "La cantidad debe ser mayor a cero.")

    return render(request, 'core/sumar_stock_form.html', {'producto': producto})
@login_required
def crear_producto_ajax(request):
    if request.method == 'POST':
        try:
            nombre = request.POST.get('nombre')
            codigo = request.POST.get('codigo_barras')
            precio = request.POST.get('precio_venta', 0)
            categoria_id = request.POST.get('categoria')
            
            # Buscamos la sucursal del usuario para asignarlo
            sucursal = obtener_sucursal_usuario(request)
            
            nuevo_p = Producto.objects.create(
                nombre=nombre,
                codigo_barras=codigo,
                precio_venta=precio,
                categoria_id=categoria_id,
                sucursal=sucursal # Para que sea Multi-Tenant
            )
            
            return JsonResponse({
                'status': 'success',
                'id': nuevo_p.id,
                'nombre': nuevo_p.nombre,
                'codigo': nuevo_p.codigo_barras
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
            
    return JsonResponse({'status': 'error', 'message': 'Método no permitido'}, status=405)

@login_required
def editar_stock(request, stock_id):
    sucursal_usuario = obtener_sucursal_usuario(request)
    stock_item = get_object_or_404(Stock, id=stock_id)

    # --- ESCUDO DE SEGURIDAD ---
    es_admin_local = hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin'
    
    if not request.user.is_superuser and not es_admin_local:
        messages.error(request, "No tenés permisos de administrador para modificar el stock.")
        return redirect('listar_productos')
    
    # Verificamos que no edite stock de OTRO local (si no es superusuario)
    if not request.user.is_superuser and stock_item.sucursal != sucursal_usuario:
        messages.error(request, "Este producto pertenece a otra sucursal.")
        return redirect('listar_productos')
    # ---------------------------

    if request.method == 'POST':
        stock_item.cantidad = int(request.POST['cantidad'])
        fecha_vencimiento = request.POST.get('fecha_vencimiento')
        stock_item.fecha_vencimiento = fecha_vencimiento if fecha_vencimiento else None
        stock_item.ubicacion = request.POST['ubicacion']
        stock_item.save()
        messages.success(request, f"Lote de {stock_item.producto.nombre} actualizado.")
        return redirect('detalle_producto_lotes', producto_id=stock_item.producto.id)

    return render(request, 'core/editar_stock.html', {'stock_item': stock_item})

def logout_view(request):
    logout(request)
    messages.success(request, "Sesión cerrada correctamente. ¡Hasta pronto!")
    return redirect('login') # O el nombre de tu pantalla de login

@login_required
def centro_precios(request):
    # Solo Admins pueden entrar acá
    es_admin = request.user.is_superuser or (hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin')
    if not es_admin:
        messages.error(request, "Acceso denegado. Exclusivo para administración de precios.")
        return redirect('dashboard')

    config = Configuracion.objects.first()
    if not config:
        config = Configuracion.objects.create(nombre_negocio="Mi Negocio")

    # Traemos solo los productos que tienen el check de "Recargo Individual" activado
    productos_rebeldes = Producto.objects.filter(aplica_recargo_individual=True).order_by('nombre')

    if request.method == 'POST':
        accion = request.POST.get('accion')

        # 1. Guardar Recargos Globales
        if accion == 'guardar_globales':
            config.recargo_credito_porcentaje = request.POST.get('recargo_credito')
            config.recargo_qr_porcentaje = request.POST.get('recargo_qr')
            config.save()
            messages.success(request, "¡Recargos globales actualizados! Aplican a todo el kiosco.")

        # 2. Quitar un producto de la lista de rebeldes
        elif accion == 'quitar_rebelde':
            prod = get_object_or_404(Producto, id=request.POST.get('producto_id'))
            prod.aplica_recargo_individual = False
            prod.save()
            messages.info(request, f"{prod.nombre} ahora usa los recargos globales.")

        # 3. Agregar un nuevo producto a la lista de rebeldes
        elif accion == 'agregar_rebelde':
            prod = get_object_or_404(Producto, id=request.POST.get('producto_id'))
            prod.aplica_recargo_individual = True
            prod.save()
            messages.success(request, f"{prod.nombre} fue separado. ¡Ahora editá sus porcentajes!")

        # 4. Actualizar los porcentajes de un producto rebelde específico
        elif accion == 'actualizar_rebelde':
            prod = get_object_or_404(Producto, id=request.POST.get('producto_id'))
            prod.recargo_credito_individual = request.POST.get('recargo_credito_ind')
            prod.recargo_qr_individual = request.POST.get('recargo_qr_ind')
            prod.save()
            messages.success(request, f"Porcentajes actualizados para {prod.nombre}.")

        return redirect('centro_precios')

    return render(request, 'core/centro_precios.html', {
        'config': config,
        'productos_rebeldes': productos_rebeldes
    })

@login_required
def buscar_clientes(request):
    query = request.GET.get('term', '')
    # Buscamos por nombre o por DNI y traemos los primeros 10 para que sea rápido
    clientes = Cliente.objects.filter(
        Q(nombre__icontains=query) | Q(dni__icontains=query)
    )[:10]

    resultados = [{
        'id': c.id, 
        'nombre': c.nombre, 
        'saldo': float(c.cuenta_corriente)
    } for c in clientes]

    return JsonResponse(resultados, safe=False)