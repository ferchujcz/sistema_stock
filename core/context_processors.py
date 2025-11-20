# core/context_processors.py
from django.utils import timezone
from django.db.models import Sum, F, Q
from .models import Stock, Producto, PerfilUsuario, Sucursal

def alertas_globales(request):
    # Si el usuario no está logueado, no mostramos alertas
    if not request.user.is_authenticated:
        return {'alertas_count': 0}

    usuario = request.user
    sucursal_usuario = None
    
    # Intentamos obtener la sucursal
    if hasattr(usuario, 'perfilusuario') and usuario.perfilusuario.sucursal:
        sucursal_usuario = usuario.perfilusuario.sucursal

    # Si no tiene sucursal y no es superadmin, no calculamos nada
    if not sucursal_usuario and not usuario.is_superuser:
         return {'alertas_count': 0}

    hoy = timezone.now().date()
    total_alertas = 0

    # 1. ALERTAS DE VENCIMIENTO (Vencidos o vencen en 20 días)
    # Filtramos por sucursal si corresponde
    stock_query = Stock.objects.filter(cantidad__gt=0)
    if sucursal_usuario and not usuario.is_superuser:
        stock_query = stock_query.filter(sucursal=sucursal_usuario)
    
    vencimientos = stock_query.filter(
        fecha_vencimiento__lte=hoy + timezone.timedelta(days=20)
    ).count()
    
    total_alertas += vencimientos

    # 2. ALERTAS DE STOCK BAJO (Total < Mínimo)
    productos_query = Producto.objects.all()
    
    # Anotamos la suma de stock (filtrada por sucursal si es necesario)
    if sucursal_usuario and not usuario.is_superuser:
        productos_con_stock = productos_query.annotate(
            stock_total=Sum('lotes__cantidad', filter=Q(lotes__sucursal=sucursal_usuario))
        )
    else:
        productos_con_stock = productos_query.annotate(
            stock_total=Sum('lotes__cantidad')
        )
    
    # Contamos cuántos tienen stock total (o 0 si es nulo) menor al mínimo
    # Coalesce es para tratar el NULL como 0, pero en Python simple:
    # Filtramos donde stock_total < stock_minimo OR stock_total es None
    stock_bajo = 0
    for p in productos_con_stock:
        actual = p.stock_total or 0
        if actual < p.stock_minimo:
            stock_bajo += 1
            
    total_alertas += stock_bajo

    # 3. ALERTAS DE SIN FECHA (Perecederos sin vencimiento)
    # Solo nos importa si hay stock positivo de ese producto sin fecha
    lotes_sin_fecha_query = Stock.objects.filter(
        producto__es_perecedero=True,
        fecha_vencimiento__isnull=True,
        cantidad__gt=0
    )
    
    if sucursal_usuario and not usuario.is_superuser:
        lotes_sin_fecha_query = lotes_sin_fecha_query.filter(sucursal=sucursal_usuario)
        
    # Contamos los productos únicos afectados, no los lotes
    sin_fecha = lotes_sin_fecha_query.values('producto').distinct().count()
    
    total_alertas += sin_fecha

    todas_sucursales = []
    sucursal_actual_nombre = "Sin Asignar"

    if request.user.is_authenticated:
        if request.user.is_superuser:
            todas_sucursales = Sucursal.objects.all()

        # Tratamos de averiguar el nombre de la sucursal actual para mostrarlo
        # (Repetimos lógica de vista por limitación de context processor, o usamos una variable de sesión si ya la seteamos)
        if request.user.is_superuser and request.session.get('sucursal_seleccionada_id'):
             try:
                 s = Sucursal.objects.get(id=request.session.get('sucursal_seleccionada_id'))
                 sucursal_actual_nombre = s.nombre
             except: pass
        elif hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.sucursal:
             sucursal_actual_nombre = request.user.perfilusuario.sucursal.nombre

    # Añadimos al return existente
    return {
        'alertas_vencimiento_count': total_alertas, # (El que ya tenías)
        'ctx_todas_sucursales': todas_sucursales,   # <-- NUEVO
        'ctx_sucursal_actual_nombre': sucursal_actual_nombre # <-- NUEVO
    }