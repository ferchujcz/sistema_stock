#from django.utils import timezone
from django.conf import settings
from django.db.models import Sum, Q
from .models import Stock, Producto, Sucursal, Configuracion
from django.utils import timezone
from django.conf import settings

def info_global(request):
    config = Configuracion.objects.first()
    if not config:
        config = Configuracion.objects.create(nombre_negocio="Mi Negocio")
    
    sucursales = []
    if request.user.is_authenticated:
        # Detectamos si es Superuser O si tiene el rol 'admin'
        es_admin = request.user.is_superuser or (hasattr(request.user, 'perfilusuario') and request.user.perfilusuario.rol == 'admin')
        if es_admin:
            sucursales = Sucursal.objects.all()

    dias_vencimiento = None
    negocio_venc = None
    if request.user.is_authenticated:
        perfil = getattr(request.user, 'perfilusuario', None)
        if perfil and perfil.negocio and perfil.negocio.fecha_vencimiento:
            negocio_venc = perfil.negocio.fecha_vencimiento
        elif request.user.is_superuser:
            from .models import Negocio
            primer_neg = Negocio.objects.first()
            if primer_neg and primer_neg.fecha_vencimiento:
                negocio_venc = primer_neg.fecha_vencimiento

    if negocio_venc:
        dias_vencimiento = (negocio_venc - timezone.localdate()).days
    else:
        fecha_venc_str = getattr(settings, 'FECHA_VENCIMIENTO_SERVIDOR', '') if 'settings' in globals() else ''
        if fecha_venc_str:
            try:
                from datetime import date
                fecha_venc = date.fromisoformat(fecha_venc_str.strip())
                dias_vencimiento = (fecha_venc - timezone.localdate()).days
            except ValueError:
                dias_vencimiento = None

    permisos_usuario = {}
    es_admin_total = False
    if request.user.is_authenticated:
        perfil = getattr(request.user, 'perfilusuario', None)
        es_admin_total = request.user.is_superuser or (perfil and perfil.rol == 'admin')
        if not es_admin_total and perfil and isinstance(perfil.permisos, dict):
            permisos_usuario = perfil.permisos

    return {
        'nombre_negocio': config.nombre_negocio,
        'todas_las_sucursales_contexto': sucursales,
        'dias_vencimiento_servidor': dias_vencimiento,
        'es_admin_total': es_admin_total,
        'permisos_usuario': permisos_usuario,
    }
def alertas_globales(request):
    """
    Calcula el número total de alertas (burbuja roja)
    respetando la sucursal que se está viendo actualmente.
    """
    # Si el usuario no está logueado, no mostramos alertas
    if not request.user.is_authenticated:
        return {'alertas_count': 0}

    usuario = request.user
    sucursal_activa = None

    # --- DETERMINAR QUÉ SUCURSAL MIRAR ---
    if usuario.is_superuser:
        # Si es Admin, miramos la sesión
        sucursal_id = request.session.get('sucursal_seleccionada_id')
        if sucursal_id:
            try:
                sucursal_activa = Sucursal.objects.get(id=sucursal_id)
            except Sucursal.DoesNotExist:
                sucursal_activa = None # Global
    elif hasattr(usuario, 'perfilusuario') and usuario.perfilusuario.sucursal:
        # Si es Empleado, miramos su perfil
        sucursal_activa = usuario.perfilusuario.sucursal
    else:
        # Usuario sin perfil ni permisos
        return {'alertas_count': 0}

    # --- INICIO DE CÁLCULOS (con caché de 60s para acelerar cada carga) ---
    from django.core.cache import cache
    cache_key = f"alertas_globales_{usuario.id}_{sucursal_activa.id if sucursal_activa else 'all'}"
    cached_val = cache.get(cache_key)
    if cached_val is not None:
        return cached_val

    hoy = timezone.now().date()
    total_alertas = 0

    # 1. ALERTAS DE VENCIMIENTO (Vencidos o vencen en 20 días)
    stock_query = Stock.objects.filter(cantidad__gt=0)
    
    if sucursal_activa:
        stock_query = stock_query.filter(sucursal=sucursal_activa)
    
    vencimientos = stock_query.filter(
        fecha_vencimiento__lte=hoy + timezone.timedelta(days=20)
    ).count()
    
    total_alertas += vencimientos

    # 2. ALERTAS DE STOCK BAJO (Total < Mínimo)
    productos_query = Producto.objects.all()
    
    if sucursal_activa:
        # Sumamos solo lotes de esa sucursal
        productos_con_stock = productos_query.annotate(
            stock_total=Sum('lotes__cantidad', filter=Q(lotes__sucursal=sucursal_activa))
        )
    else:
        # Sumamos todo (Global)
        productos_con_stock = productos_query.annotate(
            stock_total=Sum('lotes__cantidad')
        )
    
    # Contamos cuántos productos tienen stock real menor al mínimo
    stock_bajo = 0
    for p in productos_con_stock:
        actual = p.stock_total or 0
        if actual < p.stock_minimo:
            stock_bajo += 1
            
    total_alertas += stock_bajo

    # 3. ALERTAS DE SIN FECHA (Perecederos sin vencimiento cargado)
    lotes_sin_fecha_query = Stock.objects.filter(
        producto__es_perecedero=True,
        fecha_vencimiento__isnull=True,
        cantidad__gt=0
    )
    
    if sucursal_activa:
        lotes_sin_fecha_query = lotes_sin_fecha_query.filter(sucursal=sucursal_activa)
        
    sin_fecha = lotes_sin_fecha_query.values('producto').distinct().count()
    
    total_alertas += sin_fecha

    res = {'alertas_count': total_alertas}
    cache.set(cache_key, res, 60)
    return res