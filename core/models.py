# core/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone 
from datetime import timedelta 
import uuid

# =========================================================
# 1. LA TABLA PADRE (SAAS / TENANT)
# =========================================================
class Negocio(models.Model):
    nombre = models.CharField(max_length=100)
    plan = models.CharField(max_length=50, default='basico') 
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.nombre


# =========================================================
# 2. CONFIGURACIÓN, SUCURSALES Y USUARIOS
# =========================================================
class Configuracion(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS

    recargo_credito_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para pagos con tarjeta de crédito. Ej: 10.5")
    
    descuento_efectivo_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de descuento para pagos en efectivo. Ej: 5.0")
    
    recargo_qr_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para pagos con QR. Ej: 6.0")

    nombre_negocio = models.CharField(
        max_length=100, 
        default="Mi Kiosco",
        help_text="Nombre que se verá en el menú principal y tickets.")

    def __str__(self):
        return f"Config. {self.nombre_negocio}"

class Sucursal(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, related_name='sucursales', null=True) # <--- CLAVE SAAS
    nombre = models.CharField(max_length=100)
    direccion = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.nombre} ({self.negocio.nombre if self.negocio else 'Sin Negocio'})"
    
class PerfilUsuario(models.Model):
    ROLES_CHOICES = [
        ('admin', 'Administrador del Negocio'),
        ('empleado', 'Empleado de Sucursal'),
    ]
    
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfilusuario')
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, related_name='empleados', null=True) # <--- CLAVE SAAS
    sucursal = models.ForeignKey(Sucursal, on_delete=models.SET_NULL, null=True, blank=True)
    rol = models.CharField(max_length=20, choices=ROLES_CHOICES, default='empleado')

    def __str__(self):
        return f"{self.usuario.username} - {self.get_rol_display()}"


# =========================================================
# 3. ENTIDADES PRINCIPALES (Clientes, Proveedores, etc.)
# =========================================================
class Cliente(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS
    nombre = models.CharField(max_length=100)
    dni = models.CharField(max_length=20, blank=True, null=True)
    telefono = models.CharField(max_length=50, blank=True, null=True)
    direccion = models.CharField(max_length=200, blank=True, null=True)
    cuenta_corriente = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    def __str__(self):
        return self.nombre

class Proveedor(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS
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
        help_text="Saldo deudor con este proveedor (positivo = vos debés)"
    )

    def __str__(self):
        return self.nombre

    def proxima_fecha_entrega(self):
        if self.dia_semana_reparto is None or self.frecuencia_reparto is None:
            return None
        hoy = timezone.now().date()
        dias_para_proximo_dia = (self.dia_semana_reparto - hoy.weekday() + 7) % 7
        proximo_dia = hoy + timedelta(days=dias_para_proximo_dia)
        return proximo_dia

class Categoria(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS
    nombre = models.CharField(max_length=100)
    margen_ganancia_porcentaje = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Margen de ganancia sugerido para esta categoría. Ej: 30.5 para 30.5%")

    def __str__(self):
        return self.nombre

class Producto(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS
    nombre = models.CharField(max_length=200, db_index=True)
    codigo_barras = models.CharField(max_length=200, db_index=True, blank=True, null=True) # Le quité el unique=True porque ahora pueden haber códigos repetidos entre distintos negocios
    categoria = models.ForeignKey(Categoria, on_delete=models.SET_NULL, null=True, blank=True)
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True)
    costo = models.DecimalField(max_digits=10, decimal_places=2)
    precio_venta = models.DecimalField(max_digits=10, decimal_places=2)
    stock_minimo = models.PositiveIntegerField(default=5)
    es_perecedero = models.BooleanField(default=True)
    es_favorito = models.BooleanField(default=False)
# --- NUEVOS CAMPOS PARA RECARGO INDIVIDUAL (Ej: Cigarrillos) ---
    aplica_recargo_individual = models.BooleanField(
        default=False, 
        help_text="Marcar para usar recargos propios en lugar de los globales."
    )
    recargo_credito_individual = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para Tarjeta (Ej: 15.0)"
    )
    recargo_qr_individual = models.DecimalField(
        max_digits=5, decimal_places=2, default=0.0,
        help_text="Porcentaje de recargo para QR (Ej: 6.0)"
    )
    def __str__(self):
        return self.nombre

class EnvaseRetornable(models.Model):
    negocio = models.ForeignKey(Negocio, on_delete=models.CASCADE, null=True, blank=True) # <--- CLAVE SAAS
    nombre = models.CharField(max_length=100) 
    valor_deposito = models.DecimalField(max_digits=10, decimal_places=2) 

    def __str__(self):
        return self.nombre


# =========================================================
# 4. TABLAS HIJAS (Operaciones que dependen de Sucursal)
# =========================================================
class Stock(models.Model):
    UBICACION_CHOICES = [
        ('gondola', 'Góndola'),
        ('deposito', 'Depósito'),
    ]
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='lotes')
    cantidad = models.PositiveIntegerField()
    ubicacion = models.CharField(max_length=10, choices=UBICACION_CHOICES, default='deposito')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha_vencimiento = models.DateField(null=True, blank=True, db_index=True) # <-- AGREGADO
    
    def __str__(self):
        if self.fecha_vencimiento:
            return f"{self.producto.nombre} - Lote vence: {self.fecha_vencimiento}"
        return f"{self.producto.nombre} - Lote sin vencimiento"

class StockEnvases(models.Model):
    envase = models.ForeignKey(EnvaseRetornable, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    cantidad_vacia = models.PositiveIntegerField(default=0) 

    class Meta:
        unique_together = ('envase', 'sucursal') 

    def __str__(self):
        return f"{self.sucursal.nombre}: {self.cantidad_vacia} x {self.envase.nombre}"

class Venta(models.Model):
    METODO_PAGO_CHOICES = [
        ('efectivo', 'Efectivo'),
        ('debito', 'Débito'),
        ('credito', 'Crédito'),
        ('qr', 'QR'),
        ('cuenta_corriente','Cta. Cte. (Fiado)'),
        ('mixto', 'Múltiple / Mixto'),
    ]

    
    metodo_pago = models.CharField(max_length=20, choices=METODO_PAGO_CHOICES, default='efectivo')
    fecha_hora = models.DateTimeField(auto_now_add=True, db_index=True) # <-- AGREGADO
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cliente = models.ForeignKey(Cliente, on_delete=models.SET_NULL, null=True, blank=True)
    descuento_recargo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cuotas = models.PositiveIntegerField(default=1)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)

    # --- NUEVAS COLUMNAS PARA PAGOS DIVIDIDOS ---
    pago_efectivo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pago_debito = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pago_credito = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pago_qr = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pago_fiado = models.DecimalField(max_digits=10, decimal_places=2, default=0)

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


# =========================================================
# 5. PAGOS, FACTURAS Y CIERRES
# =========================================================
class PagoCliente(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name='pagos')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha = models.DateTimeField(default=timezone.now)
    monto = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Pago de {self.cliente.nombre} - ${self.monto}" # <-- BUG CORREGIDO (nombre en lugar de nombre_completo)
    
class FacturaProveedor(models.Model):
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

    total_ventas_efectivo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_ventas_tarjeta = models.DecimalField(max_digits=10, decimal_places=2, default=0) 
    total_ventas_qr = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cobros_fiado = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_pagos_proveedor = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    monto_en_caja_declarado = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Cierre en {self.sucursal.nombre} - {self.fecha_cierre_turno.strftime('%d/%m/%Y')}"

    @property
    def diferencia_caja(self):
        total_calculado_efectivo = self.total_ventas_efectivo + self.total_cobros_fiado - self.total_pagos_proveedor
        return self.monto_en_caja_declarado - total_calculado_efectivo
    

# =========================================================
# 6. HERRAMIENTAS ADICIONALES
# =========================================================
class PrediccionVenta(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    fecha = models.DateField()
    cantidad_predicha = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)

    class Meta:
        unique_together = ('producto', 'sucursal', 'fecha')

    def __str__(self):
        return f"{self.producto.nombre} ({self.sucursal.nombre}) - {self.fecha}: {self.cantidad_predicha}"

class SesionEscaneo(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ultimo_codigo = models.CharField(max_length=100, blank=True, null=True)
    codigo_procesado = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.uuid)