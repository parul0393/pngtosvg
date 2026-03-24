# PNG to SVG Docker Setup Guide

This Docker Compose configuration allows you to run the PNG to SVG conversion API in a containerized environment.

## Prerequisites

- Docker: [Install Docker](https://docs.docker.com/get-docker/)
- Docker Compose: [Install Docker Compose](https://docs.docker.com/compose/install/)

## Quick Start

### 1. Clone the Repository
```bash
cd pngtosvg
```

### 2. Set Up Environment Variables

Copy the example environment file and configure your credentials:

```bash
cp .env.example .env
```

Edit `.env` and add your actual configuration:
- Supabase credentials
- Razorpay API keys
- Payload CMS settings
- CORS origin (your frontend URL)

### 3. Build and Run

**Using Docker Compose:**

```bash
# Build the Docker image
docker-compose build

# Start the service
docker-compose up -d

# View logs
docker-compose logs -f pngtosvg-api

# Stop the service
docker-compose down
```

**Using Docker directly:**

```bash
# Build the image
docker build -t pngtosvg-api .

# Run the container
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/temp:/app/temp \
  --env-file .env \
  --name pngtosvg-api \
  pngtosvg-api
```

### 4. Verify the Service

Once running, verify the API is working:

```bash
curl http://localhost:8000/
```

You should get a response: `{"status": "API running"}`

## Project Structure

```
pngtosvg/
├── pngtosvg/
│   ├── api.py              # FastAPI application
│   ├── png_to_svg.py       # PNG to SVG conversion logic
│   ├── requirements.txt    # Python dependencies
│   └── __pycache__/
├── Dockerfile             # Docker image configuration
├── docker-compose.yml     # Docker Compose orchestration
├── .env.example          # Example environment variables
├── .dockerignore         # Files to ignore in Docker build
└── temp/                 # Output directory for converted files
```

## API Endpoints

- **GET** `/` - Health check
- **POST** `/convert` - Web conversion (requires authentication)
- **POST** `/api/convert` - API conversion (requires API key)
- **POST** `/create-order` - Create Razorpay payment order
- **POST** `/verify-payment` - Verify payment and activate subscription
- **POST** `/generate-api-key` - Generate new API key
- **GET** `/my-api-keys` - Fetch user's API keys
- **GET** `/my-api-credits` - Get API credits
- **GET** `/my-subscription` - Get active subscription
- **GET** `/my-conversions` - Fetch conversion history
- **GET** `/my-payments` - Fetch payment history

## Environment Variables Explained

| Variable | Description | Default |
|----------|-------------|---------|
| `SUPABASE_URL` | Supabase project URL | - |
| `SUPABASE_SERVICE_KEY` | Supabase service role key | - |
| `RAZORPAY_KEY_ID` | Razorpay API key ID | - |
| `RAZORPAY_KEY_SECRET` | Razorpay API key secret | - |
| `PAYLOAD_URL` | Payload CMS API endpoint | `http://localhost:3000/api` |
| `PAYLOAD_SECRET` | Payload CMS secret | - |
| `CORS_ORIGIN` | Frontend URL for CORS | `http://localhost:5173` |

## Development

To develop with live code reloading, uncomment the volume mount in `docker-compose.yml`:

```yaml
volumes:
  - ./pngtosvg:/app  # Uncomment this line
```

Then rebuild and run:

```bash
docker-compose up -d
```

## Production Deployment

For production:

1. Keep sensitive credentials in `.env` file (never commit to git)
2. Use environment-specific configurations
3. Enable resource limits in `docker-compose.yml`
4. Use a reverse proxy (Nginx/Traefik) for SSL/TLS
5. Set up proper logging and monitoring

Example production settings in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      cpus: '1'
      memory: 1G
```

## Troubleshooting

### Port Already in Use
If port 8000 is already in use:
```bash
# Change the port in docker-compose.yml
ports:
  - "8001:8000"  # Use a different port
```

### Container Crashes
Check logs:
```bash
docker-compose logs pngtosvg-api
```

### Missing Dependencies
The Dockerfile includes all required packages. If you modify `requirements.txt`, rebuild:
```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Permission Issues
For temp directory access:
```bash
chmod 777 temp/
```

## Performance Tuning

- Adjust memory limits based on image size requirements
- Increase CPU limits for faster conversions
- Use volume optimization for faster file I/O

## Cleanup

Remove all containers and volumes:
```bash
docker-compose down -v
```

Remove unused Docker images:
```bash
docker image prune
```

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [Supabase Documentation](https://supabase.com/docs)

## License

See LICENSE file in the project root.
