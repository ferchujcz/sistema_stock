# core/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

# Importamos TODOS tus modelos actualizados
from .models import (
    Negocio, Sucursal, PerfilUsuario, Configuracion, 
    Cliente, Proveedor, Categoria, Producto, Stock, 
    Venta, DetalleVenta, EnvaseRetornable, StockEnvases,
    PagoCliente, FacturaProveedor, PagoProveedor,
    CierreTurno, PrediccionVenta, SesionEscaneo
)

# =========================================================
# 1. GESTIÓN DE USUARIOS (Con Perfil Integrado)
# =========================================================
class PerfilUsuarioInline(admin.StackedInline):
    model = PerfilUsuario
    can_delete = False
    verbose_name_plural = 'Perfil del Usuario (SaaS)'
    fk_name = 'usuario' # Especifica la relación

class UserAdmin(BaseUserAdmin):
    inlines = (PerfilUsuarioInline,)

# Re-registra User con la configuración inline
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


# =========================================================
# 2. GESTIÓN DE NEGOCIO Y SUCURSALES (SaaS)
# =========================================================
# Esto mete las sucursales DENTRO de la vista del Negocio
class SucursalInline(admin.TabularInline):
    model = Sucursal
    extra = 1  # Deja 1 fila vacía para agregar rápido
    fields = ('nombre', 'direccion')

class NegocioAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'plan', 'activo', 'fecha_creacion')
    search_fields = ('nombre',)
    inlines = [SucursalInline] # ¡Acá ocurre la magia de anidarlas!

# Registramos la tabla padre del SaaS con su configuración especial
admin.site.register(Negocio, NegocioAdmin)


# =========================================================
# 3. GESTIÓN DE VENTAS
# =========================================================
class VentaAdmin(admin.ModelAdmin):
    list_display = ('id', 'fecha_hora', 'total', 'metodo_pago', 'sucursal')
    list_filter = ('metodo_pago', 'sucursal', 'fecha_hora')
    search_fields = ('id',)

admin.site.register(Venta, VentaAdmin)
admin.site.register(DetalleVenta)


# =========================================================
# 4. REGISTRO DEL RESTO DE MODELOS
# =========================================================
admin.site.register(Configuracion)
admin.site.register(Cliente)
admin.site.register(Proveedor)
admin.site.register(Categoria)
admin.site.register(Producto)
admin.site.register(Stock)

# Opcionales (para que puedas auditarlos si hace falta)
admin.site.register(EnvaseRetornable)
admin.site.register(StockEnvases)
admin.site.register(FacturaProveedor)
admin.site.register(PagoProveedor)
admin.site.register(PagoCliente)
admin.site.register(CierreTurno)
admin.site.register(SesionEscaneo)