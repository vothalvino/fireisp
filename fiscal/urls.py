from django.urls import path
from . import views
app_name='fiscal'
urlpatterns=[path('',views.index,name='index'),path('settings/',views.settings_view,name='settings'),path('verify/',views.verify,name='verify'),
    path('global/new/',views.global_create,name='global_create'),path('global/<int:pk>/',views.global_detail,name='global_detail'),path('credit/<int:pk>/issue/',views.credit_issue,name='credit_issue'),
    path('invoice/<int:pk>/',views.invoice,name='invoice'),path('<int:pk>/recover/',views.recover,name='recover'),
    path('allocation/<int:pk>/complement/',views.complement,name='complement'),path('<int:pk>/cancel/',views.cancel,name='cancel'),
    path('<int:pk>/status/',views.cancellation_status,name='cancellation_status'),path('<int:pk>/download/<str:format>/',views.download,name='download')]
