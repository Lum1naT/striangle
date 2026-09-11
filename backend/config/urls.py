from django.http import FileResponse, JsonResponse
from django.conf import settings
from django.urls import path

from trading import views

urlpatterns = [
    path("", lambda request: FileResponse(open(settings.BASE_DIR.parent / "dist/index.html", "rb"), content_type="text/html")),
    path("healthz/", views.health),
    path("api/session/", views.session),
    path("api/login/", views.login_view),
    path("api/logout/", views.logout_view),
    path("api/dashboard/", views.dashboard),
    path("api/realtime/", views.realtime),
    path("api/autonomy/control/", views.autonomy_control),
    path("api/autonomy/cycles/<uuid:cycle_id>/", views.autonomy_report),
    path("api/jobs/", views.jobs),
    path("api/jobs/<uuid:job_id>/resume/", views.resume_history),
    path("api/runs/", views.runs),
    path("api/runs/<uuid:run_id>/", views.run_detail),
    path("api/runs/<uuid:run_id>/control/", views.control),
    path("api/runs/<uuid:run_id>/readiness/", views.live_readiness),
    path("api/runs/<uuid:run_id>/fills.csv", views.export_fills),
    path("api/events/", views.export_events),
    path("api/candles.csv", views.export_candles),
]
