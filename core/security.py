from functools import wraps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

def staff_required(view):
    @wraps(view)
    @login_required
    def guarded(request, *args, **kwargs):
        if not request.user.is_staff: raise PermissionDenied
        return view(request, *args, **kwargs)
    return guarded
