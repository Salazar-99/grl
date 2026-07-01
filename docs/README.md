# GRL Docs

Static-looking documentation site built with the Next.js App Router.

## Development

```bash
npm install
npm run dev
```

## Container

```bash
docker build -f docs/Dockerfile -t grl-docs docs
docker run --rm -p 3000:3000 grl-docs
```

## Publishing

The GitHub Actions workflow publishes this app as a private registry image using repository secrets:

- `DOCS_REGISTRY`: registry host, for example `ghcr.io`
- `DOCS_REGISTRY_USERNAME`: registry username
- `DOCS_REGISTRY_PASSWORD`: registry password or token
- `DOCS_IMAGE_NAME`: fully-qualified image name without tag, for example `ghcr.io/acme/grl-docs`
