#!/bin/bash
# Build and run the GlimpseUI XCTest bridge on the booted simulator
# Usage: ./build_and_run.sh [device_name]

DEVICE=${1:-"iPhone 17 Pro"}
SCHEME="GlimpseUIBridge"
PROJECT="GlimpseUIBridge.xcodeproj"

echo "🔨 Building GlimpseUI XCTest bridge..."
echo "📱 Target: $DEVICE"

cd "$(dirname "$0")"

# Create Xcode project if it doesn't exist
if [ ! -d "$PROJECT" ]; then
    echo "📁 Creating Xcode project..."
    python3 create_project.py
fi

# Build and run tests (this starts the HTTP server on port 22087)
xcodebuild test \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -destination "platform=iOS Simulator,name=$DEVICE" \
    -only-testing:GlimpseUIBridge/GlimpseUIBridgeTests/testRunBridge \
    2>&1 | grep -E "error:|warning:|Build|Test|Bridge|✓|✗" &

echo "⏳ Waiting for bridge to start..."

# Retry for up to 60 seconds
for i in $(seq 1 30); do
    sleep 2
    if curl -s http://localhost:22087/health 2>/dev/null | grep -q '"ok":true'; then
        echo "✅ Bridge running on port 22087"
        exit 0
    fi
done

echo "❌ Bridge not responding — check simulator is booted"
