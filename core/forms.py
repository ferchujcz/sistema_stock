from django import forms
from .models import Producto

class ProductoForm(forms.ModelForm):
    class Meta:
        model = Producto
        fields = '__all__'  # Trae TODOS los campos del modelo
        # Opcional: Si querés excluir alguno que se llena solo (como fecha), usá exclude = ['fecha_creacion']
        
        widgets = {
            'es_perecedero': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'es_favorito': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ESTE ES EL SECRETO: Le ponemos la clase de estilo a todos los campos
        for field_name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs['class'] = 'form-control'