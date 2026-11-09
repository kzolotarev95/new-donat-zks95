"""Keep slow external APIs from occupying every request worker."""
bind = "127.0.0.1:8015"
workers = 2
worker_class = "gthread"
threads = 4
timeout = 30
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s %(t)s "%(m)s %(U)s" %(s)s %(b)s duration=%(L)s'
