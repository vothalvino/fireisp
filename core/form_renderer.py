from django.forms import ModelChoiceField, ModelMultipleChoiceField, Select, SelectMultiple
from django.forms.renderers import DjangoTemplates

class LookupMixin:
    """Render only the selected record; large directories are searched in bounded pages."""
    def __init__(self, kind, attrs=None):
        super().__init__({**(attrs or {}), 'data-lookup': kind})

    def optgroups(self, name, value, attrs=None):
        selected = []
        for raw in value if self.allow_multiple_selected else value[:1]:
            try:
                key = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 < key <= 9223372036854775807:
                selected.append(key)
        options = [self.create_option(name, '', 'Buscar y seleccionar…', not selected, 0)]
        iterator = self.choices
        if selected and hasattr(iterator, 'queryset'):
            for index, item in enumerate(iterator.queryset.filter(pk__in=selected), 1):
                options.append(self.create_option(name, item.pk, iterator.field.label_from_instance(item), True, index))
        return [(None, options, 0)]

class LookupSelect(LookupMixin, Select):
    pass

class LookupSelectMultiple(LookupMixin, SelectMultiple):
    pass

class FireISPFormRenderer(DjangoTemplates):
    def render(self, template_name, context, request=None):
        form = context.get('form')
        if form:
            for field in form.fields.values():
                if isinstance(field, ModelChoiceField) and not field.widget.is_hidden:
                    kind = {'core.customer': 'customer', 'core.subscription': 'subscription'}.get(field.queryset.model._meta.label_lower)
                    if kind and not isinstance(field.widget, LookupMixin):
                        widget = LookupSelectMultiple if isinstance(field, ModelMultipleChoiceField) else LookupSelect
                        field.widget = widget(kind, field.widget.attrs)
                        field.widget.choices = field.choices
        return super().render(template_name, context, request)
