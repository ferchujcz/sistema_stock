# core/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone 
from datetime import timedelta 


class Sucursal(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    direccion = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.nombre
    
    
class PerfilUsuario(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfilusuario') # Añadimos related_name
    sucursal = models.ForeignKey(Sucursal, on_delete=models.SET_NULL, null=True, blank=True, help_text="Sucursal a la que pertenece este usuario")

    def __str__(self):
        return f"Perfil de {self.usuario.username}"

class Proveedor(models.Model):
    nombre = models.CharField(max_length=200)
    telefono = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    DIA_SEMANA_CHOICES = [
        (0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'),
        (3, 'Jueves'), (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo'),
    ]
    FRECUENCIA_CHOICES = [
        (7, 'Semanal'), (14, 'Quincenal'), (30, 'Mensual'),
    ]

    nombre = models.CharField(max_length=200)
    telefono = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    # REEMPLAZAMOS el campo anterior por estos dos:
    dia_semana_reparto = models.IntegerField(
        choices=DIA_SEMANA_CHOICES, null=True, blank=True,
        help_text="Día de la semana principal en que entrega."
    )
    frecuencia_reparto = models.IntegerField(
        choices=FRECUENCIA_CHOICES, null=True, blank=True,
        help_text="Cada cuántos días entrega (aprox)."
    )

    saldo_actual = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Saldo deudor con este proveedor (positivo = vos debés, negativo = tenés crédito)"
    )

    def __str__(self):
        return self.nombre

    # (Opcional) Función para calcular la próxima entrega (la usaremos después)
    def proxima_fecha_entrega(self):
        if self.dia_semana_reparto is None or self.frecuencia_reparto is None:
            return None

        hoy = timezone.now().date()
        dias_para_proximo_dia = (self.dia_semana_reparto - hoy.weekday() + 7) % 7

        # Calculamos la fecha del próximo día de reparto
        proximo_dia = hoy + timedelta(days=dias_para_proximo_dia)

        # Si la frecuencia es semanal, esa es la fecha.
        # Si es quincenal o mensual, necesitamos más lógica (¿cuando fue la última?)
        # Por ahora, devolvemos el próximo día de la semana que toca.
        # TODO: Mejorar esta lógica para quincenal/mensual si tenemos fecha de última entrega.
        return proximo_dia
    def __str__(self):
        return self.nombre


class Categoria(models.Model):
    nombre = models.CharField(max_length=100, unique=True)
    margen_ganancia_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Margen de ganancia sugerido para esta categoría. Ej: 30.5 para 30.5%")

    def __str__(self):
        return self.nombre



class Producto(models.Model):
    nombre = models.CharField(max_length=200)
    codigo_barras = models.CharField(max_length=200, unique=True, blank=True, null=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True, blank=True)
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True)
    costo = models.DecimalField(max_digits=10, decimal_places=2)
    precio_venta = models.DecimalField(max_digits=10, decimal_places=2)
    stock_minimo = models.PositiveIntegerField(default=5, help_text="El sistema alertará cuando el stock total sea inferior a este número.")
    es_perecedero = models.BooleanField(
        default=True, 
        help_text="Marcar si este producto tiene fecha de vencimiento. (Ej: Bebidas, Golosinas). Desmarcar para (Ej: Encendedores, Vasos)."
    )
    es_favorito = models.BooleanField(
        default=False, 
        help_text="Marcar para que aparezca en la grilla de Venta Rápida"
    )

    def __str__(self):
        return self.nombre

class Stock(models.Model):
    UBICACION_CHOICES = [
        ('gondola', 'Góndola'),
        ('deposito', 'Depósito'),
    ]
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='lotes')
    cantidad = models.PositiveIntegerField()
    ubicacion = models.CharField(max_length=10, choices=UBICACION_CHOICES, default='deposito')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha_vencimiento = models.DateField(
        null=True, 
        blank=True
    )
    
    def __str__(self):
        if self.fecha_vencimiento:
            return f"{self.producto.nombre} - Lote vence: {self.fecha_vencimiento}"
        return f"{self.producto.nombre} - Lote sin vencimiento"


class Cliente(models.Model):
    nombre_completo = models.CharField(max_length=255)
    dni = models.CharField(max_length=20, blank=True, null=True, unique=True)
    telefono = models.CharField(max_length=50, blank=True, null=True)
    limite_credito = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Monto máximo que se le permite deber a este cliente."
    )

    # Este campo calculará el saldo total, es más eficiente
    saldo_actual = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return self.nombre_completo

class Venta(models.Model):
    METODO_PAGO_CHOICES = [
        ('efectivo', 'Efectivo'),
        ('debito', 'Débito'),
        ('credito', 'Crédito'),
        ('qr', 'QR'),
        ('cuenta_corriente','Cta. Cte. (Fiado)'),
    ]
    metodo_pago = models.CharField(max_length=20, choices=METODO_PAGO_CHOICES, default='efectivo')
    fecha_hora = models.DateTimeField(auto_now_add=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cliente = models.ForeignKey(Cliente, on_delete=models.SET_NULL, null=True, blank=True)
    descuento_recargo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cuotas = models.PositiveIntegerField(default=1, help_text="Número de cuotas si el pago es con crédito")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    def __str__(self):
        return f"Venta #{self.id} - {self.fecha_hora.strftime('%Y-%m-%d %H:%M')}"

class DetalleVenta(models.Model):
    venta = models.ForeignKey(Venta, related_name='detalles', on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.PositiveIntegerField()
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.cantidad} x {self.producto.nombre} en Venta #{self.venta.id}"
    # core/models.py

class Configuracion(models.Model):
    recargo_credito_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para pagos con tarjeta de crédito. Ej: 10.5")
    descuento_efectivo_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de descuento para pagos en efectivo. Ej: 5.0")
    recargo_qr_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para pagos con QR. Ej: 6.0")

    def __str__(self):
        return "Configuraciones Generales"

    # Para asegurar que solo haya una instancia de configuración
    def save(self, *args, **kwargs):
        self.pk = 1
        super(Configuracion, self).save(*args, **kwargs)

class EnvaseRetornable(models.Model):
    nombre = models.CharField(max_length=100, unique=True) # Ej: "Botella 1L Cerveza", "Cajón Gaseosa 1.5L"
    valor_deposito = models.DecimalField(max_digits=10, decimal_places=2) # Precio que se cobra/devuelve

    def __str__(self):
        return self.nombre
    
class StockEnvases(models.Model):
    envase = models.ForeignKey(EnvaseRetornable, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    cantidad_vacia = models.PositiveIntegerField(default=0) # Cantidad de envases vacíos

    class Meta:
        unique_together = ('envase', 'sucursal') # Solo una fila por envase/sucursal

    def __str__(self):
        return f"{self.sucursal.nombre}: {self.cantidad_vacia} x {self.envase.nombre}"

class PagoCliente(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='pagos')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha = models.DateTimeField(default=timezone.now)
    monto = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Pago de {self.cliente.nombre_completo} - ${self.monto}"
    
class FacturaProveedor(models.Model):
    """ Registra una factura (compra) que le debés a un proveedor. """
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name='facturas')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha_factura = models.DateField(default=timezone.now)
    numero_factura = models.CharField(max_length=100, blank=True, null=True)
    monto_total = models.DecimalField(max_digits=10, decimal_places=2)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    pagada = models.BooleanField(default=False)

    def __str__(self):
        return f"Factura {self.numero_factura} de {self.proveedor.nombre} - ${self.monto_total}"

class PagoProveedor(models.Model):
    """ Registra un pago (abono) que le hacés a un proveedor. """
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name='pagos')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha = models.DateTimeField(default=timezone.now)
    monto = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Pago a {self.proveedor.nombre} - ${self.monto}"
    
class CierreTurno(models.Model):
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    usuario_cierre = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    fecha_inicio_turno = models.DateTimeField()
    fecha_cierre_turno = models.DateTimeField(default=timezone.now)

    # Totales calculados al momento del cierre
    total_ventas_efectivo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_ventas_tarjeta = models.DecimalField(max_digits=10, decimal_places=2, default=0) # Suma de débito y crédito
    total_ventas_qr = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cobros_fiado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_pagos_proveedor = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Conteo de dinero físico
    monto_en_caja_declarado = models.DecimalField(max_digits=10, decimal_places=2, help_text="El monto que el empleado contó físicamente en la caja")

    def __str__(self):
        return f"Cierre de {self.usuario_cierre.username} en {self.sucursal.nombre} - {self.fecha_cierre_turno.strftime('%d/%m/%Y %H:%M')}"

    # Propiedad para calcular la diferencia
    @property
    def diferencia_caja(self):
        total_calculado_efectivo = self.total_ventas_efectivo + self.total_cobros_fiado - self.total_pagos_proveedor
        return self.monto_en_caja_declarado - total_calculado_efectivo
    
class PrediccionVenta(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha = models.DateField()
    cantidad_predicha = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)

    class Meta:
        # Nos aseguramos de que solo haya una predicción por producto/sucursal/día
        unique_together = ('producto', 'sucursal', 'fecha')

    def __str__(self):
        return f"{self.producto.nombre} ({self.sucursal.nombre}) - {self.fecha}: {self.cantidad_predicha}"