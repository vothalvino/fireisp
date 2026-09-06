from django.urls import path
from . import views

app_name = 'network'
urlpatterns = [
    path('', views.router_list, name='list'),
    path('access/', views.subscriber_access, name='access'),
    path('add/', views.router_create, name='create'),
    path('<int:pk>/', views.router_detail, name='detail'),
    path('<int:pk>/trust/', views.trust_host, name='trust'),
    path('<int:pk>/review/', views.review, name='review'),
    path('<int:pk>/action/<str:action>/', views.action, name='action'),
    path('jobs/<uuid:job_id>/retry/', views.retry_job, name='retry'),
    path('jobs/<uuid:job_id>/rollback/', views.rollback_job, name='rollback'),
    path('radius/authorize/', views.radius_authorize, name='radius_authorize'),
    path('radius/accounting/', views.radius_accounting, name='radius_accounting'),
]
