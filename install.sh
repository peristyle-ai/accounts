#!/usr/bin/env bash
#
# Installation script for accounts
# This script sets up the accounting system on a fresh installation
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  accounts Installation"
echo "========================================"
echo ""

# Check for PostgreSQL
if ! command -v psql &> /dev/null; then
    echo "ERROR: PostgreSQL client (psql) not found."
    echo "Please install PostgreSQL first:"
    echo "  Ubuntu/Debian: sudo apt install postgresql postgresql-contrib"
    echo "  Fedora/RHEL:   sudo dnf install postgresql-server postgresql"
    echo "  macOS:         brew install postgresql"
    exit 1
fi

# Check for Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found."
    echo "Please install Python 3.10 or later."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ] && [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
    echo ""
fi

# Activate virtual environment
VENV_BIN=""
if [ -d "venv/bin" ]; then
    VENV_BIN="venv/bin"
elif [ -d ".venv/bin" ]; then
    VENV_BIN=".venv/bin"
fi

# Install dependencies
echo "Installing Python dependencies..."
if [ -n "$VENV_BIN" ]; then
    "$VENV_BIN/pip" install -q -r requirements.txt
else
    pip3 install -q -r requirements.txt
fi
echo ""

# Copy configuration files
echo "Setting up configuration files..."

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "  Created .env from .env.example"
    fi
else
    echo "  .env already exists, skipping"
fi

if [ ! -f "config.yaml" ]; then
    if [ -f "config.yaml.example" ]; then
        cp config.yaml.example config.yaml
        echo "  Created config.yaml from config.yaml.example"
    fi
else
    echo "  config.yaml already exists, skipping"
fi

# Initialize database
echo ""
echo "Initializing PostgreSQL database..."

# Load environment variables
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-accounts}"
DB_USER="${DB_USER:-postgres}"

# Check if database exists
echo "Checking database '$DB_NAME'..."
if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
    echo "  Database '$DB_NAME' already exists."
    read -p "  Run schema initialization anyway? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "  Running schema initialization..."
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f schema/init.sql
        echo "  Schema initialized."
    else
        echo "  Skipping schema initialization."
    fi
else
    echo "  Creating database '$DB_NAME'..."
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -c "CREATE DATABASE $DB_NAME;" 2>/dev/null || \
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;"
    echo "  Running schema initialization..."
    PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f schema/init.sql
    echo "  Database created and schema initialized."
fi

# Install systemd user service
echo ""
echo "Installing systemd user service..."

SERVICE_FILE="systemd/accounts.service"
if [ -f "$SERVICE_FILE" ]; then
    USER_SERVICE_DIR="$HOME/.config/systemd/user"
    mkdir -p "$USER_SERVICE_DIR"
    cp "$SERVICE_FILE" "$USER_SERVICE_DIR/"
    echo "  Copied service file to ~/.config/systemd/user/"
    echo "  Run 'systemctl --user daemon-reload' to reload units"
else
    echo "  WARNING: Service file not found at $SERVICE_FILE"
fi

echo ""
echo "========================================"
echo "  Installation Complete!"
echo "========================================"
echo ""
echo "IMPORTANT: Please modify the following files before running:"
echo ""
echo "  1. .env"
echo "     - Set DB_PASSWORD to your PostgreSQL password"
echo ""
echo "  2. config.yaml"
echo "     - Set secret_key to a strong random value"
echo "     - Update database credentials if needed"
echo ""
echo "To start the application:"
echo "  1. Modify configuration files above"
echo "  2. Run: source venv/bin/activate"
echo "  3. Run: python run.py"
echo ""
echo "To enable the systemd service (after configuration):"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable accounts.service"
echo "  systemctl --user start accounts.service"
echo ""
echo "========================================"
