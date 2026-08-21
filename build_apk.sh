#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ANDROID_HOME="$DIR/android-sdk"
BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-34/android.jar"

APP_DIR="$DIR/android-app"
SRC_DIR="$APP_DIR/src/main/java"
RES_DIR="$APP_DIR/src/main/res"
MANIFEST="$APP_DIR/src/main/AndroidManifest.xml"
BUILD_DIR="$DIR/android-build"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/gen" "$BUILD_DIR/obj" "$BUILD_DIR/apk" "$BUILD_DIR/res"

echo "==> 1. Compiling resources with aapt2..."
"$BUILD_TOOLS/aapt2" compile --dir "$RES_DIR" -o "$BUILD_DIR/res.zip"

echo "==> 2. Linking APK with aapt2..."
"$BUILD_TOOLS/aapt2" link \
    -I "$PLATFORM" \
    -A "$APP_DIR/src/main/assets" \
    --min-sdk-version 24 \
    --target-sdk-version 34 \
    --version-code 1 \
    --version-name "1.0" \
    --manifest "$MANIFEST" \
    --java "$BUILD_DIR/gen" \
    -o "$BUILD_DIR/app-unsigned.apk" \
    --auto-add-overlay \
    "$BUILD_DIR/res.zip"

echo "==> 3. Compiling Java sources with javac..."
JAVA_FILES=$(find "$SRC_DIR" -name "*.java")
javac -encoding UTF-8 \
    -g:none \
    -source 8 -target 8 \
    -bootclasspath "$PLATFORM" \
    -cp "$PLATFORM" \
    -d "$BUILD_DIR/obj" \
    $JAVA_FILES \
    "$BUILD_DIR/gen/com/certflow/attendqr/R.java"

echo "==> 4. Dexing bytecode with d8..."
CLASS_FILES=$(find "$BUILD_DIR/obj" -name "*.class")
"$BUILD_TOOLS/d8" --output "$BUILD_DIR/apk" --lib "$PLATFORM" $CLASS_FILES

echo "==> 5. Packaging classes.dex into APK..."
cd "$BUILD_DIR/apk"
"$BUILD_TOOLS/aapt" add "$BUILD_DIR/app-unsigned.apk" classes.dex
cd "$DIR"

echo "==> 6. Aligning and Signing APK..."
"$BUILD_TOOLS/zipalign" -f -p 4 "$BUILD_DIR/app-unsigned.apk" "$BUILD_DIR/app-aligned.apk"

# Generate debug keystore if not present
KEYSTORE="$DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkey -v -keystore "$KEYSTORE" -alias attendqr \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass android -keypass android \
        -dname "CN=AttendQR, OU=Mobile, O=CertFlow, L=City, S=State, C=US"
fi

"$BUILD_TOOLS/apksigner" sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --key-pass pass:android \
    --v1-signing-enabled true \
    --v2-signing-enabled true \
    --v3-signing-enabled true \
    --out "$DIR/AttendQR.apk" \
    "$BUILD_DIR/app-aligned.apk"

# Copy to static for web download
cp "$DIR/AttendQR.apk" "$DIR/static/AttendQR.apk"

echo "=========================================="
echo "🎉 SUCCESS: APK built successfully!"
echo "📍 APK Location: $DIR/AttendQR.apk"
echo "🌐 Web Download: $DIR/static/AttendQR.apk"
echo "=========================================="
