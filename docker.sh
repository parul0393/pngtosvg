#!/bin/bash
# Docker Compose Helper Script for PNG to SVG API
# This script simplifies common Docker Compose operations

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_header() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Check prerequisites
check_prerequisites() {
    print_header "Checking Prerequisites"
    
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    print_success "Docker is installed"
    
    if ! command -v docker-compose &> /dev/null; then
        print_error "Docker Compose is not installed. Please install Docker Compose first."
        exit 1
    fi
    print_success "Docker Compose is installed"
    
    # Check if docker daemon is running
    if ! docker ps &> /dev/null; then
        print_error "Docker daemon is not running. Please start Docker."
        exit 1
    fi
    print_success "Docker daemon is running"
}

# Setup environment
setup_env() {
    print_header "Setting Up Environment"
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            print_info "Creating .env from .env.example..."
            cp .env.example .env
            print_success "Created .env file"
            print_warning "Please edit .env with your credentials"
        else
            print_error ".env.example not found!"
            exit 1
        fi
    else
        print_success ".env already exists"
    fi
}

# Build the Docker image
build() {
    print_header "Building Docker Image"
    print_info "This may take a few minutes..."
    
    if docker-compose build; then
        print_success "Docker image built successfully"
    else
        print_error "Failed to build Docker image"
        exit 1
    fi
}

# Start services
start() {
    print_header "Starting Services"
    
    if docker-compose up -d; then
        print_success "Services started successfully"
        print_info "API is running at http://localhost:8000"
        print_info "Health check: curl http://localhost:8000/"
    else
        print_error "Failed to start services"
        exit 1
    fi
}

# Stop services
stop() {
    print_header "Stopping Services"
    
    if docker-compose down; then
        print_success "Services stopped successfully"
    else
        print_error "Failed to stop services"
        exit 1
    fi
}

# View logs
logs() {
    print_header "Showing Logs"
    print_info "Press Ctrl+C to exit logs"
    docker-compose logs -f pngtosvg-api
}

# Service status
status() {
    print_header "Service Status"
    docker-compose ps
}

# Restart services
restart() {
    print_header "Restarting Services"
    
    if docker-compose restart; then
        print_success "Services restarted successfully"
    else
        print_error "Failed to restart services"
        exit 1
    fi
}

# Clean up
cleanup() {
    print_header "Cleaning Up"
    print_warning "This will remove all containers and networks"
    
    read -p "Are you sure? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if docker-compose down; then
            print_success "Cleanup completed"
        else
            print_error "Failed to cleanup"
            exit 1
        fi
    else
        print_info "Cleanup cancelled"
    fi
}

# Remove everything including volumes
cleanup_all() {
    print_header "Deep Cleanup"
    print_warning "This will remove all containers, volumes, and networks"
    
    read -p "Are you sure? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        if docker-compose down -v; then
            print_success "Deep cleanup completed"
        else
            print_error "Failed to perform deep cleanup"
            exit 1
        fi
    else
        print_info "Deep cleanup cancelled"
    fi
}

# Shell into container
shell() {
    print_header "Opening Container Shell"
    docker-compose exec pngtosvg-api /bin/bash
}

# Test API
test_api() {
    print_header "Testing API"
    
    print_info "Testing GET /"
    if curl -s http://localhost:8000/ | grep -q "API running"; then
        print_success "GET / endpoint is working"
    else
        print_error "GET / endpoint failed"
        return 1
    fi
}

# Show help
show_help() {
    echo "PNG to SVG API - Docker Compose Helper"
    echo ""
    echo "Usage: $0 [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  init        Setup environment and initial configuration"
    echo "  build       Build the Docker image"
    echo "  start       Start services in background"
    echo "  stop        Stop all services"
    echo "  restart     Restart services"
    echo "  logs        View service logs (follow mode)"
    echo "  status      Show service status"
    echo "  shell       Open shell in the running container"
    echo "  test        Test the API"
    echo "  cleanup     Remove containers and networks"
    echo "  cleanup-all Remove everything including volumes"
    echo "  help        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 init       # First time setup"
    echo "  $0 start      # Start the service"
    echo "  $0 logs       # View logs"
    echo "  $0 stop       # Stop the service"
}

# Main script
main() {
    case "${1:-help}" in
        init)
            check_prerequisites
            setup_env
            build
            start
            test_api
            print_success "Initialization complete!"
            echo ""
            print_info "Next steps:"
            echo "  1. Edit .env with your actual credentials"
            echo "  2. Restart: $0 restart"
            echo "  3. View logs: $0 logs"
            ;;
        build)
            build
            ;;
        start)
            start
            ;;
        stop)
            stop
            ;;
        restart)
            restart
            ;;
        logs)
            logs
            ;;
        status)
            status
            ;;
        shell)
            shell
            ;;
        test)
            test_api
            ;;
        cleanup)
            cleanup
            ;;
        cleanup-all)
            cleanup_all
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $1"
            show_help
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
