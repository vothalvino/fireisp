from django.contrib import admin
from .models import AuditEvent, Branch, Customer, Organization, Plan, Subscription

admin.site.site_header = 'FireISP · Administración técnica'
admin.site.site_title = 'FireISP'

@admin.register(AuditEvent)
class AuditAdmin(admin.ModelAdmin):
    list_display = ['at', 'actor', 'action', 'target']
    readonly_fields = ['at', 'actor', 'action', 'target', 'details']
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

# Business records are read-only here. The application services enforce transitions.
class RecordAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
for model in (Branch, Customer, Organization, Plan, Subscription): admin.site.register(model, RecordAdmin)
