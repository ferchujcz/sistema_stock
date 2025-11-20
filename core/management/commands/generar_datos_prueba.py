import random
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Producto, Venta, DetalleVenta, Sucursal, Cliente
from decimal import Decimal

class Command(BaseCommand):
    help = 'Genera ventas ficticias para los últimos 30 días para probar gráficos e IA'

    def handle(self, *args, **options):
        self.stdout.write("Iniciando generación de datos de prueba...")

        # Verificar requisitos
        if not Producto.objects.exists():
            self.stdout.write(self.style.ERROR("Error: Necesitas crear al menos un Producto primero."))
            return
        
        if not Sucursal.objects.exists():
            self.stdout.write(self.style.ERROR("Error: Necesitas crear al menos una Sucursal primero."))
            return

        sucursal = Sucursal.objects.first()
        productos = list(Producto.objects.all())
        
        # Vamos a generar datos para los últimos 30 días
        hoy = timezone.now()
        
        ventas_creadas = 0

        for i in range(30, -1, -1): # Desde hace 30 días hasta hoy
            fecha = hoy - timedelta(days=i)
            
            # Generar entre 1 y 5 ventas por día aleatoriamente
            cantidad_ventas_dia = random.randint(1, 5)
            
            self.stdout.write(f"Generando {cantidad_ventas_dia} ventas para el día {fecha.date()}...")

            for _ in range(cantidad_ventas_dia):
                # Elegir productos al azar para esta venta
                productos_venta = random.sample(productos, k=random.randint(1, min(len(productos), 3)))
                
                subtotal_venta = 0
                
                # Crear la venta
                venta = Venta.objects.create(
                    sucursal=sucursal,
                    metodo_pago=random.choice(['efectivo', 'debito', 'credito', 'qr']),
                    fecha_hora=fecha # Usamos la fecha simulada
                    # Los totales se actualizan abajo
                )
                
                # Forzamos la fecha (porque auto_now_add a veces la sobreescribe al crear)
                venta.fecha_hora = fecha
                venta.save()

                for prod in productos_venta:
                    cantidad = random.randint(1, 3)
                    precio = prod.precio_venta
                    subtotal_linea = precio * cantidad
                    subtotal_venta += subtotal_linea
                    
                    DetalleVenta.objects.create(
                        venta=venta,
                        producto=prod,
                        cantidad=cantidad,
                        precio_unitario=precio,
                        subtotal=subtotal_linea
                    )
                
                # Actualizamos totales de la venta
                venta.subtotal = subtotal_venta
                venta.total = subtotal_venta # Simplificado sin descuentos para la prueba
                venta.save()
                
                ventas_creadas += 1

        self.stdout.write(self.style.SUCCESS(f"¡Listo! Se crearon {ventas_creadas} ventas históricas."))
        self.stdout.write(self.style.SUCCESS("Ahora corre 'python manage.py generar_predicciones' para ver la magia."))