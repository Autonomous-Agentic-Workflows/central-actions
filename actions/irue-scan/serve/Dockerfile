# Static server for the IRUE landing page bundle on Cloud Run.
# Staged into the bundle directory by deploy-landing-page.yml; Cloud Build uses it
# when deploy-cloudrun runs with `source: <bundle>`.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --chown=nginx:nginx . /usr/share/nginx/html

# Never ship the build descriptors inside the served tree.
RUN rm -f /usr/share/nginx/html/Dockerfile /usr/share/nginx/html/nginx.conf

EXPOSE 8080
