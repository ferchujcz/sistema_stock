# core/urls.py
from django.urls import path
from . import views # Importa las vistas de tu app 'core'

urlpatterns = [
    # --- VISTAS PRINCIPALES Y DE STOCK ---
    path('stock-sucursal/<int:sucursal_id>/', views.admin_stock_por_sucursal, name='admin_stock_por_sucursal'),
    path('', views.dashboard, name='dashboard'),
    path('cambiar-sucursal/<int:sucursal_id>/', views.cambiar_sucursal_sesion, name='cambiar_sucursal_sesion'),
    path('stock/', views.stock_detalle, name='stock_detalle'),
    path('producto/<int:producto_id>/lotes/', views.detalle_producto_lotes, name='detalle_producto_lotes'),
    path('stock/nuevo/', views.agregar_stock, name='agregar_stock'),
    path('stock/<int:stock_id>/editar/', views.editar_stock, name='editar_stock'),
    path('stock/reponer/', views.reponer_gondola, name='reponer_gondola'),
    path('stock/importar/', views.importar_stock_excel, name='importar_stock'),
    path('stock/procesar-importacion/', views.procesar_importacion_excel, name='procesar_importacion_excel'),
    path('stock/plantilla-excel/', views.descargar_plantilla_excel, name='descargar_plantilla_excel'),
    path('stock/cargar-factura/', views.cargar_factura_ocr, name='cargar_factura_ocr'),
    path('stock/guardar-factura-confirmada/', views.guardar_factura_confirmada, name='guardar_factura_confirmada'),

    # --- VISTAS DE VENTAS ---
    path('ventas/nueva/', views.registrar_venta, name='registrar_venta'),
    path('ventas/historial/', views.historial_ventas, name='historial_ventas'),
    path('ventas/detalle/<int:venta_id>/', views.detalle_venta, name='detalle_venta'),
    path('analisis/canasta/', views.analisis_canasta, name='analisis_canasta'),
    path('compras/sugerencias/', views.sugerencias_compra, name='sugerencias_compra'),
    path('inventario/contar/', views.contar_inventario, name='contar_inventario'),
    path('inventario/aplicar-ajuste/', views.aplicar_ajuste_inventario, name='aplicar_ajuste_inventario'),


    # --- VISTAS DE GESTIÓN ---
    # Proveedores
    path('proveedores/', views.listar_proveedores, name='listar_proveedores'),
    path('proveedores/nuevo/', views.crear_proveedor, name='crear_proveedor'),
    path('proveedores/<int:proveedor_id>/editar/', views.editar_proveedor, name='editar_proveedor'),
    path('proveedores/<int:proveedor_id>/eliminar/', views.eliminar_proveedor, name='eliminar_proveedor'),
    path('proveedores/<int:proveedor_id>/detalle/', views.detalle_proveedor, name='detalle_proveedor'),
    path('proveedores/registrar-factura/', views.registrar_factura_proveedor, name='registrar_factura_proveedor'),
    path('proveedores/registrar-pago/', views.registrar_pago_proveedor, name='registrar_pago_proveedor'),
    # Productos
    path('productos/', views.listar_productos, name='listar_productos'),
    path('productos/nuevo/', views.crear_producto, name='crear_producto'),
    path('productos/<int:producto_id>/editar/', views.editar_producto, name='editar_producto'),
    path('productos/<int:producto_id>/eliminar/', views.eliminar_producto, name='eliminar_producto'),
    # Categorías
    path('categorias/', views.listar_categorias, name='listar_categorias'),
    path('categorias/nuevo/', views.crear_categoria, name='crear_categoria'),
    path('categorias/<int:categoria_id>/editar/', views.editar_categoria, name='editar_categoria'),
    path('categorias/<int:categoria_id>/eliminar/', views.eliminar_categoria, name='eliminar_categoria'),

    # --- RUTAS PARA CLIENTES ---
    path('clientes/', views.listar_clientes, name='listar_clientes'),
    path('clientes/nuevo/', views.crear_cliente, name='crear_cliente'),
    path('clientes/<int:cliente_id>/editar/', views.editar_cliente, name='editar_cliente'),
    path('clientes/<int:cliente_id>/estado-cuenta/', views.estado_cuenta_cliente, name='estado_cuenta_cliente'),
    path('clientes/<int:cliente_id>/registrar-pago/', views.registrar_pago_cliente, name='registrar_pago_cliente'),


    # --- NUEVAS RUTAS PARA ENVASES ---
    path('envases/', views.listar_envases, name='listar_envases'),
    path('envases/nuevo/', views.crear_envase, name='crear_envase'),
    path('envases/<int:envase_id>/editar/', views.editar_envase, name='editar_envase'),
    path('envases/<int:envase_id>/eliminar/', views.eliminar_envase, name='eliminar_envase'),

    # --- NUEVA RUTA PARA REPORTES ---
    path('reportes/', views.reportes_dashboard, name='reportes_dashboard'),

    # --- NUEVA RUTA PARA CIERRE DE TURNO ---
    path('caja/cerrar-turno/', views.cerrar_turno, name='cerrar_turno'),
    
    # --- VISTAS API ---
    path('api/buscar-productos/', views.buscar_productos, name='buscar_productos'),
    path('api/buscar-por-codigo/', views.buscar_producto_por_codigo, name='buscar_por_codigo'),
]