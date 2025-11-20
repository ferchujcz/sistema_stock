# core/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

# Importá TODOS tus modelos aquí
from .models import (
    Proveedor, Producto, Categoria, Stock, Venta, DetalleVenta, 
    Configuracion, Sucursal, PerfilUsuario
)

# Configuración para editar PerfilUsuario dentro de User
class PerfilUsuarioInline(admin.StackedInline):
    model = PerfilUsuario
    can_delete = False
    verbose_name_plural = 'Perfil (Sucursal)'
    fk_name = 'usuario' # Especifica la relación

class UserAdmin(BaseUserAdmin):
    inlines = (PerfilUsuarioInline,)

# Re-registra User con la configuración inline
admin.site.unregister(User)
admin.site.register(User, UserAdmin)

# Registra los modelos que querés ver en el admin
admin.site.register(Proveedor)
admin.site.register(Producto)
admin.site.register(Categoria)
admin.site.register(Stock) # Podrías quitarlo si ya no lo usás directo
class VentaAdmin(admin.ModelAdmin):
    # Mostramos columnas clave
    list_display = ('id', 'fecha_hora', 'total', 'metodo_pago', 'sucursal')
    # Añadimos filtros a la derecha
    list_filter = ('metodo_pago', 'sucursal', 'fecha_hora')
    # Permitimos buscar por ID
    search_fields = ('id',)

# Registramos el modelo con esta configuración especial
admin.site.register(Venta, VentaAdmin) # Podrías quitarlo si ya no lo usás directo
admin.site.register(DetalleVenta) # Podrías quitarlo si ya no lo usás directo
admin.site.register(Configuracion)
admin.site.register(Sucursal) # MUY IMPORTANTE
# No registres PerfilUsuario aquí, ya está inline en User.