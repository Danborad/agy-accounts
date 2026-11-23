# Included inside the existing 8766 server block. Keep the same web login gate.
location = /__agy-powertools-auth {
    internal;
    proxy_pass http://127.0.0.1:8765/healthz;
    proxy_method GET;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header Cookie $http_cookie;
}

location /__agy-powertools/ {
    auth_request /__agy-powertools-auth;
    client_max_body_size 64k;
    proxy_pass http://127.0.0.1:8791/;
    proxy_set_header X-Agy-Powertools-Bridge "__BRIDGE_KEY__";
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header Origin $http_origin;
    proxy_set_header Cookie "";
    proxy_set_header Connection "";
    proxy_read_timeout 20s;
    proxy_cache off;
}
