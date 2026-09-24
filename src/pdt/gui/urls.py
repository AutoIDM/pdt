from django.urls import path

from pdt.gui import views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health_grid, name="health_grid"),
    path("health/cell.json", views.health_cell, name="health_cell"),
    path("static/duckdb/<str:name>", views.duckdb_file, name="duckdb_file"),
    path("static/<str:name>", views.static_file, name="static_file"),
    path("stats/", views.stats, name="stats"),
    path("apps/<str:name>/", views.app_detail, name="app_detail"),
    path("apps/<str:name>/do/<str:action>/", views.app_action, name="app_action"),
    path("apps/<str:name>/runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("apps/<str:name>/runs/<int:pk>/artifact/", views.artifact, name="artifact"),
    path("apps/<str:name>/runs/<int:pk>/explore/", views.explore, name="explore"),
]
