import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Event(models.Model):
    """Immutable observations. received_at is when this system learned the fact."""
    source = models.CharField(max_length=30)
    source_id = models.CharField(max_length=160)
    symbol = models.CharField(max_length=20)
    kind = models.CharField(max_length=20)
    event_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    available_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "source_id", "symbol", "kind"], name="event_source_unique")]
        indexes = [models.Index(fields=["symbol", "id"]), models.Index(fields=["symbol", "kind", "received_at"])]


class Candle(models.Model):
    symbol = models.CharField(max_length=20)
    interval = models.CharField(max_length=5, default="1m")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField()
    fetched_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["symbol", "interval", "opened_at"], name="candle_unique")]


class MarketModel(models.Model):
    """Immutable, shared public-market model; never contains account data."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    symbol = models.CharField(max_length=20, db_index=True)
    version = models.CharField(max_length=64, unique=True)
    artifact = models.JSONField()
    report = models.JSONField()
    data_end = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=20)
    mode = models.CharField(max_length=15)  # paper, replay, candles, live
    status = models.CharField(max_length=20, default="running")
    config = models.JSONField()
    config_hash = models.CharField(max_length=64)
    state = models.JSONField(default=dict)
    results = models.JSONField(default=dict)
    last_event_id = models.BigIntegerField(default=0)
    entries_paused = models.BooleanField(default=False)
    flatten_requested = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True)
    validation = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    market_model = models.ForeignKey(MarketModel, null=True, blank=True, on_delete=models.PROTECT)
    error = models.TextField(blank=True)


class Decision(models.Model):
    run = models.ForeignKey(Run, related_name="decisions", on_delete=models.CASCADE)
    strategy = models.CharField(max_length=20)
    event_id = models.BigIntegerField()
    at = models.DateTimeField()
    action = models.CharField(max_length=10)
    reason = models.TextField()
    features = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "strategy", "event_id"], name="decision_once")]


class Fill(models.Model):
    run = models.ForeignKey(Run, related_name="fills", on_delete=models.CASCADE)
    strategy = models.CharField(max_length=20)
    at = models.DateTimeField()
    side = models.CharField(max_length=5)
    quantity = models.DecimalField(max_digits=36, decimal_places=16)
    price = models.DecimalField(max_digits=36, decimal_places=16)
    fee = models.DecimalField(max_digits=36, decimal_places=16)
    pnl = models.DecimalField(max_digits=36, decimal_places=16, null=True)
    reason = models.TextField()
    details = models.JSONField(default=dict)


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)
    params = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="queued")
    result = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)


class Heartbeat(models.Model):
    name = models.CharField(max_length=50, primary_key=True)
    updated_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, default="starting")
    detail = models.CharField(max_length=300, blank=True)


class LiveOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, related_name="orders", on_delete=models.PROTECT)
    client_id = models.CharField(max_length=36, unique=True)
    kind = models.CharField(max_length=10)  # entry, exit, protection
    status = models.CharField(max_length=20, default="prepared")
    request = models.JSONField()
    response = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True)
    accounted = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    attempts = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)
