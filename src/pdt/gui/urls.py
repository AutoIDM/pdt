from django.urls import path

from pdt.gui import views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health_grid, name="health_grid"),
    path("health/cell.json", views.health_cell, name="health_cell"),
    path("static/pdt.css", views.stylesheet, name="stylesheet"),
    path("apps/<str:name>/", views.app_detail, name="app_detail"),
    path("apps/<str:name>/do/<str:action>/", views.app_action, name="app_action"),
    path("apps/<str:name>/runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("apps/<str:name>/runs/<int:pk>/do/<str:action>/", views.run_action, name="run_action"),
    path("apps/<str:name>/runs/<int:pk>/artifact/", views.artifact, name="artifact"),
]
