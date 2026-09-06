from django.conf import settings
from .models import Organization

def application(request):
    org = Organization.objects.first() if request.user.is_authenticated else None
    return {'organization': org, 'app_version': settings.FIREISP_VERSION}
