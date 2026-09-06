from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from core import views

urlpatterns = [
    path('healthz', views.health, name='health'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('account/password/', auth_views.PasswordChangeView.as_view(template_name='registration/password_change.html', success_url='/'), name='password_change'),
    path('activate/<str:token>/', views.activate_account, name='activate_account'),
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
    path('billing/', include('billing.urls')),
    path('fiscal/', include('fiscal.urls')),
    path('network/', include('network.urls')),
    path('operations/', include('operations.urls')),
    path('compliance/', include('compliance.urls')),
]
