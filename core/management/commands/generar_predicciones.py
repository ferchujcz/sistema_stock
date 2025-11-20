import pandas as pd
from prophet import Prophet
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Sum
from django.db.models.functions import TruncDate
from core.models import Producto, Sucursal, Venta, DetalleVenta, PrediccionVenta
import logging

# Configuramos el logger de Prophet para que no llene la consola de mensajes técnicos
logger = logging.getLogger('cmdstanpy')
logger.addHandler(logging.NullHandler())
logger.propagate = False
logger.setLevel(logging.CRITICAL)

class Command(BaseCommand):
    help = 'Genera las predicciones de ventas para los próximos 7 días usando Prophet'

    def handle(self, *args, **options):
        hoy = timezone.now().date()
        self.stdout.write(self.style.MIGRATE_HEADING(f"--- Iniciando IA de Predicción para {hoy} ---"))

        sucursales = Sucursal.objects.all()
        productos = Producto.objects.all()

        total_predicciones = 0

        for sucursal in sucursales:
            self.stdout.write(f"Analizando Sucursal: {sucursal.nombre}...")
            
            for producto in productos:
                # 1. Obtener historial de ventas agrupado por día
                # Filtramos DetalleVenta -> Venta -> Sucursal
                ventas_diarias = DetalleVenta.objects.filter(
                    producto=producto,
                    venta__sucursal=sucursal
                ).annotate(
                    fecha=TruncDate('venta__fecha_hora')
                ).values('fecha').annotate(
                    cantidad_total=Sum('cantidad')
                ).order_by('fecha')

                # Necesitamos un mínimo de datos históricos para que la IA funcione
                # (mínimo 5 días con ventas para que no falle matemáticamente)
                if ventas_diarias.count() < 5:
                    continue

                # 2. Preparar datos para Prophet (Pandas DataFrame)
                df = pd.DataFrame(list(ventas_diarias))
                
                if df.empty:
                    continue

                # Prophet exige columnas llamadas 'ds' (fecha) y 'y' (valor)
                df = df.rename(columns={'fecha': 'ds', 'cantidad_total': 'y'})

                try:
                    # 3. Configurar y Entrenar el Modelo
                    # daily_seasonality=False porque no tenemos datos hora a hora suficientes
                    # weekly_seasonality=True es CLAVE para kioscos (viernes != lunes)
                    m = Prophet(daily_seasonality=False, weekly_seasonality=True, yearly_seasonality=False)
                    m.fit(df)

                    # 4. Predecir el Futuro (7 días)
                    future = m.make_future_dataframe(periods=7)
                    forecast = m.predict(future)

                    # 5. Procesar y Guardar Resultados
                    # Filtramos solo las predicciones futuras
                    predicciones_futuras = forecast[forecast['ds'].dt.date >= hoy]

                    nuevas_predicciones = []
                    for index, row in predicciones_futuras.iterrows():
                        fecha_prediccion = row['ds'].date()
                        # 'yhat' es el valor predicho. Usamos max(0, ...) porque no existen ventas negativas
                        cantidad = round(max(0, row['yhat']), 2)

                        nuevas_predicciones.append(
                            PrediccionVenta(
                                producto=producto,
                                sucursal=sucursal,
                                fecha=fecha_prediccion,
                                cantidad_predicha=cantidad
                            )
                        )

                    # Transacción atómica: Borrar viejas y guardar nuevas para este producto/sucursal
                    # Esto evita duplicados si corres el comando dos veces
                    PrediccionVenta.objects.filter(
                        producto=producto, 
                        sucursal=sucursal, 
                        fecha__gte=hoy
                    ).delete()
                    
                    PrediccionVenta.objects.bulk_create(nuevas_predicciones)
                    total_predicciones += 1
                    # Descomentar para ver detalle en consola (puede ser mucho texto)
                    # self.stdout.write(f"   -> Predicción OK: {producto.nombre}")

                except Exception as e:
                    self.stderr.write(f"   -> Error en {producto.nombre}: {e}")

        self.stdout.write(self.style.SUCCESS(f"¡Listo! Se generaron predicciones para {total_predicciones} productos/sucursal."))