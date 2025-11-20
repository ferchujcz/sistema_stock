# control_stock/urls.py
from django.contrib import admin
from django.urls import path, include # Asegurate que include esté importado

urlpatterns = [
    # Ruta para el panel de administración de Django
    path('admin/', admin.site.urls),

    # Rutas para el sistema de login/logout de Django
    path('accounts/', include('django.contrib.auth.urls')),

    # Incluye TODAS las URLs de tu aplicación 'core'
    path('', include('core.urls')),
]