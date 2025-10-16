#!/usr/bin/env python3
"""
Recipe Scraper UI Launcher
A simple script to start the Recipe Scraper web interface
"""

import os
import sys
import subprocess
import webbrowser
import time
from pathlib import Path

app = Flask(__name__)

def check_dependencies():
    """Check if required packages are installed"""
    # Map package names to their import names
    package_imports = {
        'flask': 'flask',
        'flask-cors': 'flask_cors',
        'requests': 'requests', 
        'beautifulsoup4': 'bs4',
        'openai': 'openai',
        'yt-dlp': 'yt_dlp'
    }
    
    missing_packages = []
    
    for package, import_name in package_imports.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print("❌ Missing required packages:")
        for package in missing_packages:
            print(f"   - {package}")
        print("\n💡 Install missing packages with:")
        print(f"   pip install {' '.join(missing_packages)}")
        return False
    
    return True

def check_groq_api_key():
    """Check if Groq API key is available"""
    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        print("⚠️  GROQ_API_KEY environment variable not set")
        print("💡 Set it with: export GROQ_API_KEY='your_api_key_here'")
        print("🔄 Using default key from script (may have rate limits)")
    else:
        print("✅ GROQ_API_KEY found")
    
    return True

# ✅ Import and expose Flask app for Gunicorn
try:
    from recipe_scraper_s3 import app
except ImportError:
    print("❌ Could not import Flask app")
    print("💡 Make sure 'recipe_scraper_s3.py' is in the same directory")
    sys.exit(1)

def start_flask_app():
    """Start the Flask application"""
    try:
        print("🍳 Starting Recipe Scraper UI...")
        print("📺 Supports YouTube videos and web recipes!")
        print("🌐 Opening browser to: http://localhost:5000")
        print("=" * 50)

        # ✅ Skip browser launch on Render
        if os.getenv("RENDER") != "true":
            def open_browser():
                time.sleep(1.5)
                webbrowser.open('http://localhost:5000')
            import threading
            threading.Thread(target=open_browser, daemon=True).start()

        # Start Flask app locally
        app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False)

    except Exception as e:
        print(f"❌ Error starting application: {e}")
        sys.exit(1)

def main():
    """Main function"""
    print("🚀 Recipe Scraper UI Launcher")
    print("=" * 40)
    
    # Check current directory
    current_dir = Path.cwd()
    print(f"📁 Working directory: {current_dir}")
    
    # Count existing recipe files
    recipe_files = list(current_dir.glob('recipe_*.md'))
    print(f"📄 Found {len(recipe_files)} existing recipe files")
    
    # Check dependencies
    print("\n🔍 Checking dependencies...")
    if not check_dependencies():
        sys.exit(1)
    
    print("✅ All dependencies found")
    
    # Check API key
    print("\n🔑 Checking API configuration...")
    check_groq_api_key()
    
    # Start the application
    print("\n🎯 Starting application...")
    try:
        start_flask_app()
    except KeyboardInterrupt:
        print("\n👋 Shutting down gracefully...")
        print("Thanks for using Recipe Scraper!")

# ✅ Entry point for CLI
if __name__ == "__main__":
    main()

# ✅ Expose app for Gunicorn
# Gunicorn will look for this when running: gunicorn launch_scraper:app
# app = app
