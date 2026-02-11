#!/bin/bash
# Setup script for Instagram Vendor Scraper

echo "🚀 Instagram Vendor Scraper Setup"
echo "=========================================="
echo ""

# Check if API token is provided
if [ "$1" = "" ]; then
    echo "Usage: ./setup.sh YOUR_APIFY_API_TOKEN"
    echo ""
    echo "To get your API token:"
    echo "  1. Go to https://apify.com"
    echo "  2. Sign up for a free account"
    echo "  3. Go to Settings → Integrations"
    echo "  4. Copy your API token"
    echo ""
    exit 1
fi

API_TOKEN=$1

# Create .env file
cat > .env << EOF
export APIFY_API_TOKEN="$API_TOKEN"
EOF

echo "✓ Created .env file with API token"
echo ""

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. source .env"
echo "  2. python scraper.py"
echo ""
