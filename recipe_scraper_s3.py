#!/usr/bin/env python3

from fileinput import filename
import glob
from multiprocessing import context
import os
import sys
import subprocess
import webbrowser
import json
import boto3
from datetime import datetime
from flask import Flask, redirect, render_template, render_template_string, jsonify, request, session, url_for
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse
import openai
import yt_dlp
from dotenv import load_dotenv
from botocore.exceptions import ClientError, NoCredentialsError
from PIL import Image, ImageOps
import pytesseract
import traceback
from auth import auth_bp
from models import User, Recipe, db
# from admin import admin_bp
from flask_migrate import Migrate
from flask_login import LoginManager, current_user, login_user, logout_user, login_required
from flask_sqlalchemy import SQLAlchemy




load_dotenv()

app = Flask(__name__)
# CORS(app)

# Use local SQLite DB directly, no env vars
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.secret_key = os.getenv("SECRET_KEY", "default_secret")

# db = SQLAlchemy(app)


migrate = Migrate(app, db)
db.init_app(app)

# app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(auth_bp)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login' 

with app.app_context():
    db.create_all()


class S3Storage:
    def __init__(self):
        self.bucket_name = os.getenv('AWS_S3_BUCKET')
        if not self.bucket_name:
            raise ValueError("AWS_S3_BUCKET environment variable is required")
        
        try:
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                region_name=os.getenv('AWS_REGION', 'us-east-1')
            )
            # Test connection
            self.s3_client.head_bucket(Bucket=self.bucket_name)
        except (NoCredentialsError, ClientError) as e:
            raise ValueError(f"AWS S3 configuration error: {str(e)}")
    
# In class S3Storage:
    def save_recipe(self, filename, content, recipe_name, user_id):
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{user_id}/{filename}",  # <--- Uses user_id
                Body=content.encode('utf-8'),
                ContentType='text/markdown',
                Metadata={
                    'created': datetime.now().isoformat(),
                    'type': 'recipe',
                    'recipe-name': recipe_name
                }
            )
            return True
        except ClientError:
            return False
        
        
    def get_recipe(self, filename, user_id):
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{user_id}/{filename}"  # <--- Uses user_id
            )
            return response['Body'].read().decode('utf-8')
        except ClientError:
            return None
        
    def get_recipe_metadata(self, key):
        """
        Helper function to get just the metadata and last-modified time of an S3 object.
        This uses a fast HEAD request instead of downloading the whole file.
        """
        try:
            response = self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=key
            )
            # S3 metadata keys are auto-lowercased, so 'recipe-name' is correct
            return response.get('Metadata', {}), response.get('LastModified')
        except ClientError:
            return None, None
    
# In class S3Storage:
    def list_recipes(self, user_id):
        try:
            response = self.s3_client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=f"recipes/{user_id}/recipe_"  # <--- Uses user_id
            )
            
            recipes = []
            for obj in response.get('Contents', []):
                filename = obj['Key'].replace(f'recipes/{user_id}/', '') 
                if not filename.endswith('.md'):
                    continue
                    
                try:
                    metadata, last_modified = self.get_recipe_metadata(obj['Key'])
                    
                    if metadata and 'recipe-name' in metadata:
                        recipe_name = metadata.get('recipe-name', 'Unknown Recipe')
                        created = metadata.get('created', last_modified.isoformat() if last_modified else datetime.now().isoformat())
                    else:
                        content = self.get_recipe(filename, user_id) 
                        if content and content.startswith('# '):
                            recipe_name = content.split('\n')[0][2:].strip()
                        else:
                            recipe_name = "Unknown Recipe"
                        created = obj['LastModified'].isoformat()

                    recipes.append({
                        'filename': filename,
                        'name': recipe_name,
                        'created': created
                    })
                except Exception as e:
                    print(f"Failed to process {filename}: {e}")
                    continue
        
            return sorted(recipes, key=lambda x: x['created'], reverse=True)
        except ClientError:
            return []
        

    # In class S3Storage:

    def list_all_recipes_admin(self):
        """
        Admin-only function to list all recipes from all users.
        """
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(
                Bucket=self.bucket_name,
                Prefix="recipes/"
            )
            
            recipes = []
            # This dict will store counts like {'user_id_1': 10, 'user_id_2': 5}
            user_recipe_counts = {} 

            for page in pages:
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    # Path is "recipes/USER_ID/FILENAME"
                    # We must ignore the "folder" itself
                    if key.endswith('/'):
                        continue

                    parts = key.split('/')
                    # Ensure the path is valid (recipes/user_id/filename)
                    if len(parts) != 3 or not parts[2].startswith('recipe_'):
                        continue 

                    user_id = parts[1]
                    filename = parts[2]

                    try:
                        # Update this user's recipe count
                        user_recipe_counts[user_id] = user_recipe_counts.get(user_id, 0) + 1

                        # Get metadata (fast)
                        metadata, last_modified = self.get_recipe_metadata(obj['Key'])
                        
                        if metadata and 'recipe-name' in metadata:
                            recipe_name = metadata.get('recipe-name', 'Unknown Recipe')
                            created = metadata.get('created', last_modified.isoformat() if last_modified else datetime.now().isoformat())
                        else:
                            # Slow fallback for old files (should be rare)
                            content = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)['Body'].read().decode('utf-8')
                            if content and content.startswith('# '):
                                recipe_name = content.split('\n')[0][2:].strip()
                            else:
                                recipe_name = "Unknown Recipe"
                            created = obj['LastModified'].isoformat()

                        recipes.append({
                            'filename': filename,
                            'name': recipe_name,
                            'created': created,
                            'user_id': user_id  # Add user_id for the admin view
                        })
                    except Exception as e:
                        print(f"Failed to process admin recipe {filename}: {e}")
                        continue
            
            sorted_recipes = sorted(recipes, key=lambda x: x['created'], reverse=True)
            # Return both the list and the counts dictionary
            return sorted_recipes, user_recipe_counts

        except ClientError as e:
            print(f"Admin recipe list failed: {e}")
            return [], {}

    def delete_recipe(self, filename, user_id):
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{user_id}/{filename}"  # <--- Uses user_id
            )
            return True
        except ClientError:
            return False
class RecipeScraper:
    def __init__(self, storage):
        api_key = os.getenv('GROQ_API_KEY')
        if not api_key:
            raise ValueError("GROQ_API_KEY environment variable is required")
        
        self.ai_client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1"
        )
        
        self.storage = storage
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    # @app.route('/admin/dashboard')
    # def admin_dashboard():
    #     username = current_user.username
    #     users = User.query.all()
    #     limits = {
    #         'admin': 10,
    #         'family': 5,
    #         'user': 3
    #     }
    #     return render_template('admin_dashboard.html', username=username, users=users, limits=limits)

    # @app.route('/user/dashboard')
    # def user_dashboard():
    #     return render_template('user_dashboard.html')

    # @app.route('/family/dashboard')
    # def family_dashboard():
    #     return render_template('family_dashboard.html')


# from flask import request, session, redirect, url_for
# from flask_login import login_user
      
 
    def is_youtube_url(self, url):
        youtube_patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)',
            r'youtube\.com.*[?&]v=',
            r'youtu\.be/'
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in youtube_patterns)
    
    def extract_youtube_transcript(self, url):
        try:
            cookie_file_path = 'youtube_cookies.txt'
            ydl_opts = {
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en', 'en-US', 'en-GB'],
                'skip_download': True,
                'no_warnings': True,
                # Add this line to specify the cookie file:
                'cookiefile': cookie_file_path
            }
            if not os.path.exists(cookie_file_path):
                print(f"Warning: Cookie file not found at {cookie_file_path}. Authentication may fail.")
             # Decide if you want to remove the cookiefile option or proceed without it
             # del ydl_opts['cookiefile']
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get('title', 'YouTube Recipe')
                duration = info.get('duration', 0)
                
                subtitles = info.get('subtitles', {}) or info.get('automatic_captions', {})
                transcript_text = ""
                
                for lang in ['en', 'en-US', 'en-GB', 'a.en']:
                    if lang in subtitles:
                        for subtitle in subtitles[lang]:
                            if subtitle.get('ext') == 'vtt':
                                try:
                                    subtitle_response = requests.get(subtitle['url'])
                                    vtt_content = subtitle_response.text
                                    transcript_text = self.parse_vtt_content(vtt_content)
                                    break
                                except:
                                    continue
                        if transcript_text:
                            break
                
                if not transcript_text:
                    transcript_text = info.get('description', '')
                
                return {
                    "url": url,
                    "title": title,
                    "duration": duration,
                    "content": transcript_text,
                    "type": "youtube_video",
                    "scraped_at": datetime.now().isoformat()
                }
                
        except Exception:
            return None
    
    def parse_vtt_content(self, vtt_content):
        lines = vtt_content.split('\n')
        text_lines = []
        
        for line in lines:
            line = line.strip()
            if (not line or 
                line.startswith('WEBVTT') or 
                line.startswith('NOTE') or
                '-->' in line or
                re.match(r'^\d+$', line)):
                continue
            
            line = re.sub(r'<[^>]+>', '', line)
            line = re.sub(r'&\w+;', '', line)
            
            if line:
                text_lines.append(line)
        
        return ' '.join(text_lines)
    
    def extract_recipe_sections(self, content):
        lines = content.split('\n')
        ingredients_section = []
        instructions_section = []
        
        in_ingredients = False
        in_instructions = False
        
        for line in lines:
            line = line.strip()
            
            if re.search(r'\bingredients?\b', line, re.IGNORECASE) and len(line) < 100:
                in_ingredients = True
                in_instructions = False
                continue
            
            if re.search(r'\b(instructions?|method|directions?|steps?)\b', line, re.IGNORECASE) and len(line) < 100:
                in_instructions = True
                in_ingredients = False
                continue
            
            if in_ingredients:
                if re.search(r'\b(method|instructions?|directions?|steps?|nutrition|notes)\b', line, re.IGNORECASE):
                    in_ingredients = False
                    in_instructions = 'instructions' in line.lower() or 'method' in line.lower()
                    continue
                
                if line and not line.startswith(('▢', '•', '-', '*')):
                    if any(indicator in line.lower() for indicator in ['g ', 'ml', 'tbsp', 'tsp', 'cup', 'oz', 'lb', 'clove', 'onion', 'garlic']):
                        ingredients_section.append(line)
                elif line.startswith(('▢', '•', '-', '*')):
                    ingredients_section.append(line[1:].strip())
            
            if in_instructions:
                if re.search(r'\b(nutrition|notes|tips|faq)\b', line, re.IGNORECASE):
                    in_instructions = False
                    continue
                
                if line:
                    if (re.match(r'^\d+\.?\s+', line) or 
                        line.lower().startswith('step') or
                        any(action in line.lower() for action in ['cook', 'add', 'heat', 'stir', 'mix', 'drain', 'serve', 'fry', 'bake'])):
                        instructions_section.append(line)
        
        return {
            'ingredients': ingredients_section,
            'instructions': instructions_section
        }
    
    def scrape_url(self, url):
        if self.is_youtube_url(url):
            return self.extract_youtube_transcript(url)
        
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            for element in soup(["script", "style", "nav", "header", "footer"]):
                element.decompose()
            
            structured_recipe = self.extract_structured_data(soup)
            
            title = soup.find('title')
            page_title = title.get_text().strip() if title else ""
            
            text_content = soup.get_text()
            lines = (line.strip() for line in text_content.splitlines())
            text_content = '\n'.join(line for line in lines if line)
            
            recipe_sections = self.extract_recipe_sections(text_content)
            
            return {
                "url": url,
                "title": page_title,
                "content": text_content[:15000],
                "structured_data": structured_recipe,
                "recipe_sections": recipe_sections,
                "scraped_at": datetime.now().isoformat()
            }
            
        except Exception:
            return None
    
    def extract_structured_data(self, soup):
        scripts = soup.find_all('script', type='application/ld+json')
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    data = data[0]
                if data.get('@type') == 'Recipe' or 'Recipe' in str(data.get('@type', '')):
                    return data
            except:
                continue
        return None

    def parse_with_ai(self, scraped_data):
        import json

        content_text = scraped_data.get('content', '').strip()

        # Step 1: Inject pre-extracted sections if available
        recipe_sections = scraped_data.get('recipe_sections', {})
        if recipe_sections.get('ingredients') or recipe_sections.get('instructions'):
            sections_text = (
                f"\nPRE-EXTRACTED INGREDIENTS:\n{chr(10).join(recipe_sections.get('ingredients', []))}"
                f"\n\nPRE-EXTRACTED INSTRUCTIONS:\n{chr(10).join(recipe_sections.get('instructions', []))}"
            )
            content_text = sections_text + "\n\nFULL PAGE CONTENT:\n" + content_text

        # Step 2: Add structured data if present
        if scraped_data.get('structured_data'):
            structured_info = json.dumps(scraped_data['structured_data'], indent=2)
            content_text = f"STRUCTURED DATA:\n{structured_info}\n\n{content_text}"

        # Step 3: Contextualize source type
        source_type = scraped_data.get('type')
        if source_type == 'youtube_video':
            video_context = 'This is a transcript from a YouTube cooking video.'
            video_rules = '- For video transcripts: ignore "like and subscribe", introductions, and off-topic chat'
        elif source_type == 'photo_ocr':
            video_context = 'This is OCR text extracted from a photo of a recipe.'
            video_rules = '- For OCR text: ignore any misread characters, focus on extracting the recipe content'
        else:
            video_context = 'This is from a recipe webpage.'
            video_rules = ''

        # Step 4: Build prompt
        prompt = f"""You're a recipe extraction expert. Extract ONLY the essential recipe info from this content.

    {video_context}

    CRITICAL: You MUST include ALL cooking steps. Do not skip any steps, even if they seem minor.

    Return in this EXACT format:
    # [Recipe Name]

    **Ingredients:**
    • [ingredient 1]
    • [ingredient 2]
    ...

    **Method:**
    1. [step 1]
    2. [step 2]
    3. [step 3]
    ...

    EXTRACTION RULES:
    - Convert ALL measurements to METRIC: grams (g), ml, litres, Celsius (°C)
    - Examples: "225g flour", "500ml milk", "180°C", "2 tbsp = 30ml"
    - Keep ingredient format: "225g plain flour" not "flour (225g)"
    - Include EVERY cooking step - do not combine or skip steps
    - Include ESSENTIAL cooking details: temperatures, times, visual cues, doneness indicators
    - Examples: "brown until golden", "rest 30 minutes", "cook until internal temp 74°C", "simmer until thickened"
    - Convert Fahrenheit to Celsius: 375°F = 190°C, 165°F = 74°C
    - Keep steps direct but include critical timing/visual cues
    - Remove fluff, ads, life stories, nutrition info, but keep ALL technical cooking steps
    - Look carefully through the content for ALL method/instructions/steps
    - Pay special attention to pre-extracted ingredients and instructions sections
    - Ignore navigation, comments, ratings, related recipes, subscription offers
    {video_rules}
    - If no clear recipe exists, return only: "NO_RECIPE_FOUND"
    - Don't include URL in output
    - Be thorough - include every step mentioned in the original recipe

    DOUBLE-CHECK: Ensure you haven't missed any cooking steps from the original recipe.

    URL: {scraped_data.get('url', 'N/A')}

    Content:
    {content_text}
    """

        # Step 5: Call AI model
        try:
            response = self.ai_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a recipe extraction expert specializing in converting cooking content into clean, minimalist, metric-based recipes. Your priority is capturing ALL cooking steps and ingredients without omission. Focus on thoroughness and accuracy."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0,
                max_tokens=5000,
                stream=False
            )
            ai_response = response.choices[0].message.content.strip()
            return ai_response

        except Exception as e:
            print("AI parsing failed:", str(e))
            return self.fallback_parse(scraped_data)   
        

    def fallback_parse(self, scraped_data):
        content = scraped_data['content']
        structured = scraped_data.get('structured_data') or {}
        recipe_sections = scraped_data.get('recipe_sections', {})
        
        title = structured.get('name') or scraped_data['title'].split('|')[0].strip()
        
        ingredients = structured.get('recipeIngredient', [])
        if not ingredients and recipe_sections.get('ingredients'):
            ingredients = recipe_sections['ingredients']
        
        instructions = []
        if structured.get('recipeInstructions'):
            structured_instructions = structured['recipeInstructions']
            if isinstance(structured_instructions, list):
                for inst in structured_instructions:
                    if isinstance(inst, dict):
                        instructions.append(inst.get('text', str(inst)))
                    else:
                        instructions.append(str(inst))
        elif recipe_sections.get('instructions'):
            instructions = recipe_sections['instructions']
        
        if not ingredients and not instructions:
            return "NO_RECIPE_FOUND"
        
        formatted_ingredients = '\n'.join('• ' + ing for ing in ingredients[:20]) if ingredients else '• No ingredients found'
        formatted_instructions = '\n'.join(f'{i+1}. {inst}' for i, inst in enumerate(instructions[:15])) if instructions else '1. No instructions found'
        
        return f"""# {title}

**Ingredients:**
{formatted_ingredients}

**Method:**
{formatted_instructions}"""
    
    def create_markdown(self, ai_response, scraped_data):
        if ai_response.strip() == "NO_RECIPE_FOUND":
            return f"""# No Recipe Found

            **URL:** {scraped_data['url']}

            Could not extract a clear recipe from this URL. The page may not contain a recipe or may be behind a paywall."""
        
        if scraped_data['url'] not in ai_response:
            lines = ai_response.split('\n')
            if lines and not ai_response.strip() == "NO_RECIPE_FOUND":
                title_line = lines[0]
                rest = '\n'.join(lines[1:]) if len(lines) > 1 else ''
                ai_response = f"{title_line}\n\n**URL:** {scraped_data['url']}\n\n{rest}"
        
        return ai_response
    

# In class RecipeScraper:
    # In class RecipeScraper:
    
    def scrape_and_save(self, url, user_id):
        scraped_data = self.scrape_url(url)
        if not scraped_data or not scraped_data.get('content'):
            return {"status": "failed", "error": "Failed to scrape URL", "url": url}

        ai_response = None 
        try:
            ai_response = self.parse_with_ai(scraped_data)
            print("AI Response:", repr(ai_response))
        except Exception as e:
            print(f"An error occurred during AI parsing: {str(e)}")
            traceback.print_exc()
            return {"status": "failed", "error": f"AI parsing failed: {str(e)}", "url": url}

        if not ai_response or ai_response.strip() == "NO_RECIPE_FOUND":
            return {"status": "failed", "error": "AI failed to extract recipe", "url": url}

        markdown_content = self.create_markdown(ai_response, scraped_data)
        if not markdown_content or len(markdown_content.strip()) < 10:
            return {"status": "failed", "error": "Failed to format recipe content", "url": url}

        recipe_name = "Unknown Recipe"
        first_line = markdown_content.split('\n')[0].strip()
        if first_line.startswith('# '):
            recipe_name = first_line[2:].strip()

        domain = urlparse(url).netloc.replace('www.', '').replace('/', '_')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"recipe_{domain}_{timestamp}.md"
        print("Saving to S3:", filename, "for user:", user_id)

        save_success = self.storage.save_recipe(
            filename, 
            markdown_content, 
            recipe_name, 
            user_id  # <--- Passes user_id
        )
        
        if not save_success:
            return {"status": "failed", "error": "Failed to save recipe to S3", "url": url}

        return {
            "status": "success",
            "filename": filename,
            "recipe_name": recipe_name,
            "url": url,
            "content": markdown_content,
            "created": datetime.now().isoformat()
        }
    

try:
    storage = S3Storage()
    scraper = RecipeScraper(storage)
except ValueError as e:
    print(f"Configuration error: {e}")
    exit(1)

HTML_TEMPLATE = """
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Churro's Recipes</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.2/marked.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/tesseract.js/4.1.1/tesseract.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #0a0a0a;
            color: #fafafa;
            line-height: 1.5;
        }

        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 2rem;
            min-height: 100vh;
        }

        .header {
            margin-bottom: 2rem;
            text-align: center;
        }

        .header h1 {
            font-size: 2rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
            background: linear-gradient(to right, #fafafa, #a3a3a3);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .header p {
            color: #a3a3a3;
            font-size: 0.95rem;
        }

        .add-recipe-section {
            background: #161616;
            border: 1px solid #262626;
            border-radius: 12px;
            padding: 2rem;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
        }

        .section-title {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
            color: #fafafa;
        }

        .section-description {
            color: #a3a3a3;
            font-size: 0.9rem;
            margin-bottom: 1.5rem;
        }

        .input-group {
            margin-bottom: 1rem;
        }

        .input {
            width: 100%;
            padding: 0.75rem 1rem;
            background: #0a0a0a;
            border: 1px solid #404040;
            border-radius: 8px;
            color: #fafafa;
            font-size: 0.95rem;
            transition: all 0.2s ease;
        }

        .input:focus {
            outline: none;
            border-color: #fafafa;
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.1);
        }

        .input::placeholder {
            color: #737373;
        }

        .textarea {
            width: 100%;
            padding: 0.75rem 1rem;
            background: #0a0a0a;
            border: 1px solid #404040;
            border-radius: 8px;
            color: #fafafa;
            font-size: 0.9rem;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            resize: vertical;
            min-height: 400px;
            transition: all 0.2s ease;
        }

        .textarea:focus {
            outline: none;
            border-color: #fafafa;
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.1);
        }

        .btn {
            padding: 0.75rem 1.5rem;
            background: #fafafa;
            color: #0a0a0a;
            border: none;
            border-radius: 8px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            font-size: 0.95rem;
            text-decoration: none;
            display: inline-block;
        }

        .btn:hover {
            background: #e5e5e5;
            transform: translateY(-1px);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }

        .btn-secondary {
            background: #262626;
            color: #fafafa;
            border: 1px solid #404040;
        }

        .btn-secondary:hover {
            background: #404040;
        }

        .btn-danger {
            background: #ef4444;
            color: #fafafa;
        }

        .btn-danger:hover {
            background: #dc2626;
        }

        .btn-success {
            background: #10b981;
            color: #fafafa;
        }

        .btn-success:hover {
            background: #059669;
        }

        .btn-small {
            padding: 0.5rem 1rem;
            font-size: 0.85rem;
        }

        .progress-bar {
            width: 100%;
            height: 4px;
            background: #262626;
            border-radius: 2px;
            overflow: hidden;
            margin: 1rem 0;
        }

        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #fafafa, #a3a3a3);
            border-radius: 2px;
            transition: width 0.3s ease;
            width: 0%;
        }

        .collection-header {
            margin-bottom: 1.5rem;
        }

        .recipe-item {
            background: #161616;
            border: 1px solid #262626;
            border-radius: 8px;
            margin-bottom: 0.5rem;
            cursor: pointer;
            transition: all 0.3s ease;
            overflow: hidden;
        }

        .recipe-item:hover {
            border-color: #404040;
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }

        .recipe-item.expanded {
            border-color: #fafafa;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.4);
            transform: none;
        }

        .recipe-header {
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .recipe-info {
            flex: 1;
        }

        .recipe-name {
            font-weight: 500;
            color: #fafafa;
            margin-bottom: 0.25rem;
            font-size: 1.1rem;
        }

        .recipe-meta {
            font-size: 0.85rem;
            color: #737373;
        }

        .recipe-actions {
            display: flex;
            gap: 0.5rem;
            align-items: center;
        }

        .expand-icon {
            color: #737373;
            font-size: 1.2rem;
            transition: transform 0.3s ease;
            margin-left: 0.5rem;
        }

        .recipe-item.expanded .expand-icon {
            transform: rotate(90deg);
        }

        .recipe-content {
            padding: 0 1rem 1rem 1rem;
            background: #0a0a0a;
            animation: slideDown 0.3s ease-out;
        }

        @keyframes slideDown {
            from {
                opacity: 0;
                transform: translateY(-10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .recipe-content h1 {
            color: #fafafa;
            margin-bottom: 1rem;
            font-size: 1.5rem;
        }

        .recipe-content h2 {
            color: #fafafa;
            margin: 1.5rem 0 0.75rem 0;
            font-size: 1.2rem;
        }

        .recipe-content strong {
            color: #fafafa;
        }

        .recipe-content ul, .recipe-content ol {
            margin-left: 1.5rem;
            margin-bottom: 1rem;
        }

        .recipe-content li {
            margin-bottom: 0.5rem;
            color: #d4d4d4;
            line-height: 1.6;
        }

        .recipe-content p {
            margin-bottom: 1rem;
            color: #d4d4d4;
            line-height: 1.6;
        }

        .recipe-content a {
            color: #60a5fa;
            text-decoration: none;
        }

        .recipe-content a:hover {
            text-decoration: underline;
        }

        .empty-state {
            text-align: center;
            padding: 3rem 1rem;
            color: #737373;
        }

        .empty-state h3 {
            margin-bottom: 0.5rem;
            color: #a3a3a3;
        }

        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(4px);
        }

        .modal.show {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .modal-content {
            background: #161616;
            border: 1px solid #262626;
            border-radius: 12px;
            padding: 2rem;
            width: 90%;
            max-width: 800px;
            max-height: 90vh;
            overflow-y: auto;
            animation: modalSlideIn 0.3s ease-out;
        }

        @keyframes modalSlideIn {
            from {
                opacity: 0;
                transform: scale(0.9);
            }
            to {
                opacity: 1;
                transform: scale(1);
            }
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }

        .modal-title {
            font-size: 1.5rem;
            font-weight: 600;
            color: #fafafa;
        }

        .close-btn {
            background: none;
            border: none;
            color: #737373;
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.5rem;
            line-height: 1;
        }

        .close-btn:hover {
            color: #fafafa;
        }

        .modal-actions {
            display: flex;
            gap: 1rem;
            justify-content: flex-end;
            margin-top: 1.5rem;
        }

        .confirm-dialog {
            background: #161616;
            border: 1px solid #262626;
            border-radius: 12px;
            padding: 2rem;
            width: 90%;
            max-width: 400px;
            text-align: center;
            animation: modalSlideIn 0.3s ease-out;
        }

        .confirm-dialog h3 {
            color: #fafafa;
            margin-bottom: 1rem;
        }

        .confirm-dialog p {
            color: #a3a3a3;
            margin-bottom: 2rem;
        }

        .confirm-actions {
            display: flex;
            gap: 1rem;
            justify-content: center;
        }

        @media (max-width: 768px) {
            .container {
                padding: 1rem;
            }
            
            .add-recipe-section {
                padding: 1.5rem;
            }

            .recipe-actions {
                flex-direction: column;
            }

            .modal-content {
                padding: 1.5rem;
                margin: 1rem;
            }

            .modal-actions {
                flex-direction: column;
            }
        }
            .container {
              padding-top: 5rem !important;
              padding: 2rem;
              font-family: Arial, sans-serif;
            }
    .header {
      margin-bottom: 2rem;
    }
    .auth-button {
      position: absolute;
      top: 1rem;
      right: 1rem;
    }
    .auth-button a {
      text-decoration: none;
      padding: 0.5rem 1rem;
      border-radius: 5px;
      color: white;
    }
    .logout {
      background-color: #dc3545;
    }
    .login {
      background-color: #007bff;
    }


    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🍳 Churro's Recipes</h1>
            <p>Manage your scraped recipes with a modern interface</p>
        </div>
        <div>

        
        <div style="position: absolute; top: 20px; right: 20px; display: flex; gap: 10px;">
            {% if current_user.is_authenticated %}
                <a href="{{ url_for('dashboard') }}"
                style="text-decoration: none; padding: 10px 20px; background-color: #2980b9; color: white; border-radius: 5px; font-weight: 500;">
                Dashboard
                </a>
                <a href="{{ url_for('auth.logout') }}"
                style="text-decoration: none; padding: 10px 20px; background-color: #c0392b; color: white; border-radius: 5px; font-weight: 500;">
                Logout
                </a>
            {% else %}
                <a href="{{ url_for('auth.login') }}"
                style="text-decoration: none; padding: 10px 20px; background-color: #444; color: white; border-radius: 5px; font-weight: 500;">
                Login
                </a>
            {% endif %}
        </div>

        <!-- Add Recipe from URL Section -->
        <div class="add-recipe-section">
            <h2 class="section-title">Add Recipe from URL</h2>
            <p class="section-description">Scrape a new recipe from any website or YouTube video</p>

            <div class="input-group">
                <input 
                    type="text" 
                    class="input" 
                    id="urlInput" 
                    placeholder="Enter recipe URL (website or YouTube)..."
                >
            </div>

            <button class="btn" id="scrapeBtn" onclick="scrapeRecipe()">
                <span id="scrapeText">🔍 Scrape Recipe</span>
            </button>

            <div class="progress-bar" id="progressBar" style="display: none;">
                <div class="progress-fill" id="progressFill"></div>
            </div>
        </div>

        <!-- OCR Recipe Section -->
       <div class="add-recipe-section">
        <h2 class="section-title">Add Recipe from Photo</h2>
        <p class="section-description">Upload a photo of a recipe or take a picture with your camera</p>

        <div class="input-group">
            <!-- ✅ Updated input: allows both camera and gallery -->
            <input 
            type="file" 
            class="input" 
            id="imageInput" 
            accept="image/*"
            style="display: none;"
            >

            <!-- Trigger button -->
            <button class="btn btn-secondary" onclick="document.getElementById('imageInput').click()" style="width: 100%; margin-bottom: 1rem;">
            📷 Choose Photo
            </button>

            <!-- Image preview -->
            <div id="imagePreview" style="display: none; margin-bottom: 1rem;">
            <img id="previewImg" style="max-width: 100%; max-height: 300px; border-radius: 8px; border: 1px solid #404040;">
            </div>

            <!-- OCR text area -->
            <textarea 
            id="ocrText" 
            class="textarea" 
            placeholder="OCR text will appear here. You can edit it before processing..."
            style="min-height: 200px; display: none;"
            ></textarea>
        </div>

        <!-- OCR trigger button -->
        <button class="btn" id="processOcrBtn" onclick="processOcrRecipe()" style="display: none;">
            <span id="processOcrText">🔄 Extract Recipe</span>
        </button>

        <!-- Progress bar -->
        <div class="progress-bar" id="ocrProgressBar" style="display: none;">
            <div class="progress-fill" id="ocrProgressFill"></div>
        </div>
        </div>

            <button class="btn" id="processOcrBtn" onclick="processOcrRecipe()" style="display: none;">
                <span id="processOcrText">🔄 Extract Recipe</span>
            </button>

            <div class="progress-bar" id="ocrProgressBar" style="display: none;">
                <div class="progress-fill" id="ocrProgressFill"></div>
            </div>
        </div>
        
        <!-- Recipe Collection -->
        <div class="collection-header">
            <h2 class="section-title">Recipe Collection</h2>
            <p class="section-description">Click any recipe to expand and view details</p>
        </div>

        <div id="recipeList">
            <div class="empty-state">
                <h3>No recipes found</h3>
                <p>Add your first recipe by entering a URL above</p>
            </div>
        </div>

        <button class="btn btn-secondary" onclick="refreshRecipeList()" style="margin-top: 1rem; width: 100%;">
            📁 Refresh List
        </button>
    </div>

    <!-- Edit Modal -->
    <div id="editModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title" id="editModalTitle">Edit Recipe</h2>
                <button class="close-btn" onclick="closeEditModal()">&times;</button>
            </div>
            
            <div class="input-group">
                <textarea id="editContent" class="textarea" placeholder="Edit your recipe in Markdown format..."></textarea>
            </div>
            
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeEditModal()">Cancel</button>
                <button class="btn btn-success" id="saveRecipeBtn" onclick="saveRecipe()">💾 Save Changes</button>
            </div>
        </div>
    </div>

    <!-- Confirm Delete Modal -->
    <div id="deleteModal" class="modal">
        <div class="confirm-dialog">
            <h3>Delete Recipe</h3>
            <p>Are you sure you want to delete this recipe? This action cannot be undone.</p>
            <div class="confirm-actions">
                <button class="btn btn-secondary" onclick="closeDeleteModal()">Cancel</button>
                <button class="btn btn-danger" onclick="confirmDelete()">🗑️</button>
            </div>
        </div>
    </div>

    <script>
        let recipes = [];
        let expandedRecipe = null;
        let editingFilename = null;
        let deletingFilename = null;

        async function getRecipeList() {
            try {
                const response = await fetch('/api/recipes');
                return await response.json();
            } catch (error) {
                console.error('Failed to fetch recipes:', error);
                return [];
            }
        }

        async function getRecipeContent(filename) {
            try {
                const response = await fetch(`/api/recipe/${encodeURIComponent(filename)}`);
                const data = await response.json();
                return data.content;
            } catch (error) {
                console.error('Failed to fetch recipe content:', error);
                return null;
            }
        }

        async function scrapeRecipeFromUrl(url) {
            const response = await fetch('/api/scrape', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ url: url })
            });
            
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to scrape recipe');
            }
            
            return await response.json();
        }

        

        async function saveRecipeContent(filename, content) {
            const response = await fetch('/api/recipe/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ 
                    filename: filename, 
                    content: content 
                })
            });
            
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to save recipe');
            }
            
            return await response.json();
        }

        async function deleteRecipeFile(filename) {
            const response = await fetch(`/api/recipe/${encodeURIComponent(filename)}`, {
                method: 'DELETE'
            });
            
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to delete recipe');
            }
            
            return await response.json();
        }

        async function loadRecipeList() {
            try {
                recipes = await getRecipeList();
                renderRecipeList();
            } catch (error) {
                console.error('Failed to load recipes:', error);
            }
        }
        async function shareRecipe(name, url, event) {
            event.stopPropagation(); // Prevent toggleRecipe from firing

            try {
                const filename = url.split('/').pop(); // Extract filename from URL
                const response = await fetch(`/api/recipe/${encodeURIComponent(filename)}`);
                const data = await response.json();
                const recipeText = data.content;

                const sharePayload = {
                    title: name,
                    text: `📖 ${name}\n\n${recipeText}`
                };

                if (navigator.share) {
                    navigator.share(sharePayload).catch(err => {
                        console.error('Share failed:', err);
                        copyToClipboard(sharePayload.text);
                    });
                } else {
                    copyToClipboard(sharePayload.text);
                }
            } catch (error) {
                console.error('Failed to fetch recipe content:', error);
                alert('Could not share recipe. Try again later.');
            }
        }
        async function loadRecipes() {
            const response = await fetch('/api/recipes');
            const recipes = await response.json();

            const container = document.getElementById('recipe-list');
            container.innerHTML = ''; // Clear previous

            if (recipes.length === 0) {
                container.innerHTML = '<p>No recipes found.</p>';
                return;
            }

            recipes.forEach(recipe => {
                const item = document.createElement('div');
                item.className = 'recipe-item';
                item.innerHTML = `
                <strong>${recipe.name}</strong><br>
                <small>${new Date(recipe.created).toLocaleString()}</small><br>
                <button onclick="viewRecipe('${recipe.filename}')">View</button>
                <hr>
                `;
                container.appendChild(item);
            });
            }

            async function viewRecipe(filename) {
            const response = await fetch(`/api/recipe/${filename}`);
            const data = await response.json();

            const viewer = document.getElementById('recipe-view');
            viewer.innerHTML = `<pre>${data.content}</pre>`;
            }

        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert('Recipe copied to clipboard!');
            }).catch(err => {
                console.error('Clipboard copy failed:', err);
                alert('Failed to copy recipe to clipboard.');
            });
        }

        function fallbackCopy(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert('Recipe copied to clipboard!');
            }).catch(err => {
                console.error('Clipboard copy failed:', err);
            });
        }

        function renderRecipeList() {
            const listElement = document.getElementById('recipeList');

            if (recipes.length === 0) {
                listElement.innerHTML = `
                    <div class="empty-state">
                        <h3>No recipes found</h3>
                        <p>Add your first recipe by entering a URL above</p>
                    </div>
                `;
                return;
            }

            listElement.innerHTML = recipes.map(recipe => `
                <div class="recipe-item ${expandedRecipe?.filename === recipe.filename ? 'expanded' : ''}" 
                    onclick="toggleRecipe('${recipe.filename}')">
                    <div class="recipe-header">
                        <div class="recipe-info">
                            <div class="recipe-name">${recipe.name}</div>
                            <div class="recipe-meta">
                                ${new Date(recipe.created).toLocaleDateString()} • ${recipe.filename}
                            </div>
                        </div>
                        <div class="recipe-actions">
                            <button class="btn btn-small btn-secondary" onclick="editRecipe('${recipe.filename}', event)" title="Edit Recipe">
                                ✏️
                            </button>
                            <button class="btn btn-small btn-danger" onclick="deleteRecipe('${recipe.filename}', event)" title="Delete Recipe">
                                🗑️
                            </button>
                            <button class="btn btn-small btn-outline-primary" 
                                    onclick="shareRecipe('${recipe.name}', '${window.location.origin}/recipe/${recipe.filename}', event)" 
                                    title="Share Recipe">
                                🔗
                            </button>
                            <div class="expand-icon">
                                ${expandedRecipe?.filename === recipe.filename ? '▼' : '▶'}
                            </div>
                        </div>
                    </div>
                    ${expandedRecipe?.filename === recipe.filename ? `
                        <div class="recipe-content" id="content-${recipe.filename}">
                            <div style="text-align: center; padding: 2rem; color: #737373;">
                                Loading recipe...
                            </div>
                        </div>
                    ` : ''}
                </div>
            `).join('');

            // Load content for expanded recipe
            if (expandedRecipe) {
                loadRecipeContent(expandedRecipe.filename);
            }
        }

        async function toggleRecipe(filename) {
            if (expandedRecipe?.filename === filename) {
                // Collapse if already expanded
                expandedRecipe = null;
            } else {
                // Expand new recipe
                expandedRecipe = recipes.find(r => r.filename === filename);
            }
            
            renderRecipeList();
        }

        async function loadRecipeContent(filename) {
            try {
                const content = await getRecipeContent(filename);
                if (content) {
                    // Configure marked for better list rendering
                    marked.setOptions({
                        breaks: true,
                        gfm: true
                    });
                    
                    const html = marked.parse(content);
                    const contentElement = document.getElementById(`content-${filename}`);
                    if (contentElement) {
                        contentElement.innerHTML = html;
                    }
                } else {
                    throw new Error('No content received');
                }
            } catch (error) {
                const contentElement = document.getElementById(`content-${filename}`);
                if (contentElement) {
                    contentElement.innerHTML = `
                        <div style="text-align: center; padding: 2rem; color: #ef4444;">
                            <h3>Error loading recipe</h3>
                            <p>Failed to load the recipe content</p>
                        </div>
                    `;
                }
            }
        }

        async function editRecipe(filename, event) {
            event.stopPropagation();
            
            try {
                const content = await getRecipeContent(filename);
                if (content) {
                    editingFilename = filename;
                    document.getElementById('editContent').value = content;
                    document.getElementById('editModal').classList.add('show');
                } else {
                    alert('Failed to load recipe content for editing');
                }
            } catch (error) {
                console.error('Failed to load recipe for editing:', error);
                alert('Failed to load recipe for editing');
            }
        }

        async function saveRecipe() {
            if (!editingFilename) return;
            
            const content = document.getElementById('editContent').value;
            if (!content.trim()) {
                alert('Recipe content cannot be empty');
                return;
            }
            
            try {
                await saveRecipeContent(editingFilename, content);
                closeEditModal();
                await loadRecipeList();
                
                // Re-expand the recipe if it was expanded before
                if (expandedRecipe?.filename === editingFilename) {
                    renderRecipeList();
                }
                
                // Show success message briefly
                const saveBtn = document.querySelector('.btn-success');
                const originalText = saveBtn.textContent;
                saveBtn.textContent = '✅ Saved!';
                setTimeout(() => {
                    saveBtn.textContent = originalText;
                }, 2000);
                
            } catch (error) {
                console.error('Failed to save recipe:', error);
                alert('Failed to save recipe: ' + error.message);
            }
        }

        function deleteRecipe(filename, event) {
            event.stopPropagation();
            deletingFilename = filename;
            document.getElementById('deleteModal').classList.add('show');
        }

        async function confirmDelete() {
            if (!deletingFilename) return;
            
            try {
                await deleteRecipeFile(deletingFilename);
                closeDeleteModal();
                
                // If the deleted recipe was expanded, clear the expansion
                if (expandedRecipe?.filename === deletingFilename) {
                    expandedRecipe = null;
                }
                
                await loadRecipeList();
            } catch (error) {
                console.error('Failed to delete recipe:', error);
                alert('Failed to delete recipe: ' + error.message);
            }
        }

        function closeEditModal() {
            document.getElementById('editModal').classList.remove('show');
            editingFilename = null;
        }

        function closeDeleteModal() {
            document.getElementById('deleteModal').classList.remove('show');
            deletingFilename = null;
        }

        async function scrapeRecipe() {
            const url = document.getElementById('urlInput').value.trim();
            if (!url) {
                alert('Please enter a URL');
                return;
            }

            const scrapeBtn = document.getElementById('scrapeBtn');
            const scrapeText = document.getElementById('scrapeText');
            const progressBar = document.getElementById('progressBar');
            const progressFill = document.getElementById('progressFill');

            scrapeBtn.disabled = true;
            scrapeText.textContent = '🔄 Scraping...';
            progressBar.style.display = 'block';
            
            let progress = 0;
            const progressInterval = setInterval(() => {
                progress += Math.random() * 15;
                if (progress > 90) progress = 90;
                progressFill.style.width = progress + '%';
            }, 300);

            try {
                const response = await fetch('/api/scrape', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });

                clearInterval(progressInterval);
                progressFill.style.width = '100%';

              // --- FIX: Check for login redirect BEFORE trying .json() ---
              const contentType = response.headers.get("content-type");
              if (response.status === 401 || (response.redirected && !contentType?.includes("application/json")) || (!response.ok && !contentType?.includes("application/json"))) {
                  // If status is 401 OR it redirected to non-JSON OR it's an error with non-JSON
                  alert("Please login first."); // Show the login message
                  window.location.href = "{{ url_for('auth.login') }}"; // Redirect to login
                  scrapeText.textContent = '🔒 Login Required'; // Update button text
                  return; // Stop processing
              }
              // --- End Fix ---

                if (!response.ok) {
                  // Handle other JSON errors (like validation errors from the API)
                    const errorData = await response.json(); 
                    throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
                }

                // If we reach here, it was a successful JSON response
                const result = await response.json(); 
                
                await loadRecipeList();
                scrapeText.textContent = '✅ Recipe Added!';

                document.getElementById('urlInput').value = '';
                setTimeout(() => {
                    scrapeText.textContent = '🔍 Scrape Recipe';
                }, 2000);
                
            } catch (error) {
                clearInterval(progressInterval);
              // Check if the error is the JSON parsing error specifically
              if (error instanceof SyntaxError && error.message.includes("Unexpected token '<'")) {
                 alert("Please login first."); // Show login message for this specific error too
                 window.location.href = "{{ url_for('auth.login') }}"; 
                 scrapeText.textContent = '🔒 Login Required';
              } else {
                 // Handle other errors normally
                 scrapeText.textContent = '❌ Failed to Scrape';
                 setTimeout(() => {
                     scrapeText.textContent = '🔍 Scrape Recipe';
                 }, 2000);
                 console.error('Scraping failed:', error);
                 alert('Failed to scrape recipe: ' + error.message);
              }
            } finally {
                scrapeBtn.disabled = false;
                setTimeout(() => {
                    progressBar.style.display = 'none';
                    progressFill.style.width = '0%';
                }, 1000);
            }
        }
        

        // OCR Functions
        document.getElementById('imageInput').addEventListener('change', function(e) {
            const file = e.target.files[0];
            if (file && file.type.startsWith('image/')) {
                // Show preview
                const reader = new FileReader();
                reader.onload = function(e) {
                    const previewImg = document.getElementById('previewImg');
                    const imagePreview = document.getElementById('imagePreview');
                    previewImg.src = e.target.result;
                    imagePreview.style.display = 'block';
                    
                    // Start OCR processing
                    performOCR(file);
                };
                reader.readAsDataURL(file);
            }
        });

        async function performOCR(imageFile) {
            const ocrText = document.getElementById('ocrText');
            const processOcrBtn = document.getElementById('processOcrBtn');
            const ocrProgressBar = document.getElementById('ocrProgressBar');
            const ocrProgressFill = document.getElementById('ocrProgressFill');
            
            try {
                // Show progress
                ocrProgressBar.style.display = 'block';
                ocrText.value = 'Processing image...';
                ocrText.style.display = 'block';
                
                // Perform OCR using Tesseract.js
                const { data: { text } } = await Tesseract.recognize(
                    imageFile,
                    'eng',
                    {
                        logger: m => {
                            if (m.status === 'recognizing text') {
                                const progress = Math.round(m.progress * 100);
                                ocrProgressFill.style.width = progress + '%';
                            }
                        }
                    }
                );
                
                // Complete progress
                ocrProgressFill.style.width = '100%';
                
                // Display extracted text
                ocrText.value = text.trim();
                processOcrBtn.style.display = 'block';
                
                setTimeout(() => {
                    ocrProgressBar.style.display = 'none';
                    ocrProgressFill.style.width = '0%';
                }, 1000);
                
            } catch (error) {
                console.error('OCR failed:', error);
                ocrText.value = 'OCR processing failed. Please try a clearer image or type the recipe manually.';
                setTimeout(() => {
                    ocrProgressBar.style.display = 'none';
                    ocrProgressFill.style.width = '0%';
                }, 1000);
            }
        }

        async function processOcrRecipe() {
            const text = document.getElementById('ocrText').value.trim();
            if (!text) {
                alert('No text to process. Please upload an image first.');
                return;
            }
            
            if (text.includes('OCR processing failed') || text.includes('Processing image...')) {
                alert('Please wait for OCR processing to complete or upload a new image.');
                return;
            }

            const processBtn = document.getElementById('processOcrBtn');
            const processText = document.getElementById('processOcrText');
            const progressBar = document.getElementById('ocrProgressBar');
            const progressFill = document.getElementById('ocrProgressFill');

            // Update UI
            processBtn.disabled = true;
            processText.textContent = '🔄 Extracting Recipe...';
            progressBar.style.display = 'block';
            
            // Simulate progress
            let progress = 0;
            const progressInterval = setInterval(() => {
                progress += Math.random() * 15;
                if (progress > 90) progress = 90;
                progressFill.style.width = progress + '%';
            }, 300);

            try {
                const response = await fetch('/api/ocr', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ text: text })
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.error || 'Failed to process OCR text');
                }
                
                const result = await response.json();
                
                // Complete progress
                clearInterval(progressInterval);
                progressFill.style.width = '100%';
                
                // Refresh recipes list
                await loadRecipeList();
                
                // Clear inputs
                document.getElementById('imageInput').value = '';
                document.getElementById('ocrText').value = '';
                document.getElementById('imagePreview').style.display = 'none';
                document.getElementById('ocrText').style.display = 'none';
                document.getElementById('processOcrBtn').style.display = 'none';
                
                // Success message
                processText.textContent = '✅ Recipe Added!';
                setTimeout(() => {
                    processText.textContent = '🔄 Extract Recipe';
                }, 2000);
                
            } catch (error) {
                clearInterval(progressInterval);
                processText.textContent = '❌ Failed to Extract';
                setTimeout(() => {
                    processText.textContent = '🔄 Extract Recipe';
                }, 2000);
                console.error('OCR processing failed:', error);
                alert('Failed to extract recipe: ' + error.message);
            } finally {
                processBtn.disabled = false;
                setTimeout(() => {
                    progressBar.style.display = 'none';
                    progressFill.style.width = '0%';
                }, 1000);
            }
        }

        function refreshRecipeList() {
            loadRecipeList();
        }

        // Close modals when clicking outside
        window.addEventListener('click', function(event) {
            const editModal = document.getElementById('editModal');
            const deleteModal = document.getElementById('deleteModal');
            
            if (event.target === editModal) {
                closeEditModal();
            }
            if (event.target === deleteModal) {
                closeDeleteModal();
            }
        });

        // Enter key support for URL input
        document.getElementById('urlInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                scrapeRecipe();
            }
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                closeEditModal();
                closeDeleteModal();
            }
            
            if (e.ctrlKey && e.key === 's' && editingFilename) {
                e.preventDefault();
                saveRecipe();
            }
        });

        // Initialize
        loadRecipeList();
    </script>
</body>
</html>"""


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
    
  

@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    username = current_user.username # Get username from current_user
    
    users = User.query.all() # Get all users from SQL DB
    recipes = []
    user_recipe_counts = {}

    try:
        # Check the user's role
        if role == 'admin':
            # Admin gets ALL recipes from ALL users in S3
            recipes, user_recipe_counts = storage.list_all_recipes_admin()
        else:
            # Regular user gets ONLY their recipes from S3
            recipes = storage.list_recipes(current_user.id)
            # For a non-admin, we just build their own count
            user_recipe_counts[str(current_user.id)] = len(recipes)
            
        # Total recipes is the length of the list we just got
        total_recipes = len(recipes) 
        
        # Add the S3-based recipe count to each user object
        for user in users:
            # Get count from the dict we built, default to 0
            user.recipe_count = user_recipe_counts.get(str(user.id), 0) 

        # Correctly query the DB for active users
        active_users = User.query.filter_by(is_active=True).count()
        
        # Calculate average using the S3 counts
        total_s3_recipes = sum(user_recipe_counts.values())
        avg_recipes = round(total_s3_recipes / len(users), 2) if users else 0

        # This context now has the correct data from S3
        context = {
            'username': username,
            'total_recipes': total_recipes,
            'active_users': active_users,
            'last_sync_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'top_source': 'youtube.com', # Placeholder
            'popular_tags': ['Chicken', 'Quick Meals'], # Placeholder
            'avg_recipes': avg_recipes,
            'recipes': recipes, # This is now the S3 recipe list
            'users': users,
        }

        if role == 'admin':
            return render_template('admin_dashboard.html', **context)
        elif role == 'family':
            return render_template('family_dashboard.html', **context)
        elif role == 'user':
            return render_template('user_dashboard.html', **context)
        else:
            return redirect(url_for('auth.login'))
            
    except Exception as e:
        print(f"Error loading dashboard: {e}")
        traceback.print_exc()
        # If the dashboard fails, log the user out to be safe
        return redirect(url_for('auth.logout'))
# @app.route('/update-user-roles', methods=['POST'])
# @login_required
# def update_user_roles():
#         if current_user.role.lower() != 'admin':
#             return redirect('/dashboard')

#         users = User.query.all()
#         for user in users:
#             new_role = request.form.get(f'role_{user.id}')
#             if new_role and new_role != user.role:
#                 user.role = new_role
#         db.session.commit()
#         return redirect('/dashboard')
    

@app.route('/auth/login', methods=['POST'])
def login():
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.password == password:
            login_user(user)

            # Normalize and store role from DB
            role = user.role.strip().lower()
            session['role'] = role

            return redirect('/dashboard')

        # Optional: handle login failure
        return redirect('/login')


        # return "Invalid credentials", 401

@app.route('/auth/register', methods=['POST'])
def register():
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role', 'user')

        if User.query.filter_by(username=username).first():
            return "Username already exists", 400

        
        new_user = User(username=username, password=password, role=role)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('auth_page'))

@app.route('/auth/logout', methods=['GET'])
def logout():
    logout_user()
    return redirect(url_for('auth_page'))
    
@app.route('/api/update-role', methods=['POST'])
def update_role():
    data = request.get_json()
    user_id = data.get('user_id')
    new_role = data.get('new_role')

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    user.role = new_role
    db.session.commit()
    return jsonify({'message': 'Role updated successfully'})

@app.route('/api/dashboard-metrics')
def dashboard_metrics():
    total_recipes = Recipe.query.count()
    active_users = User.query.count()
    last_sync_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    return jsonify({
        'total_recipes': total_recipes,
        'active_users': active_users,
        'last_sync_time': last_sync_time
    })

@app.route('/api/usage-analytics')
def usage_analytics():
    # Most scraped source
    top_source = db.session.query(Recipe.source).group_by(Recipe.source).order_by(db.func.count().desc()).first()

    # Average recipes per user
    user_count = User.query.count()
    recipe_count = Recipe.query.count()
    avg_recipes = round(recipe_count / user_count, 2) if user_count else 0

    # Popular tags (placeholder logic)
    popular_tags = ['quick', 'vegan', 'dessert']  # Replace with actual tag aggregation if available

    # Breakdown for charting
    scraped_count = Recipe.query.filter(Recipe.source != None).count()
    manual_count = Recipe.query.filter(Recipe.source == None).count()
    favorites_count = 10  # Replace with actual logic if you track favorites

    return jsonify({
        'top_source': top_source[0] if top_source else 'N/A',
        'avg_recipes': avg_recipes,
        'popular_tags': popular_tags,
        'scraped_count': scraped_count,
        'manual_count': manual_count,
        'favorites_count': favorites_count,
        'total_recipes': recipe_count,
        'total_users': user_count
    })

@app.route('/api/users')
def get_users():
    users = User.query.all()
    return jsonify([
        {
            'id': u.id,
            'username': u.username,
            'recipe_count': len(u.recipes),
            'role': u.role 
        } for u in users
    ])



@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/recipes')
@login_required
def get_recipes():
    """
    Get list of all markdown recipe files from S3 bucket with metadata.
    """
    try:
        # YOUR S3Storage.list_recipes() function ALREADY does all the work:
        # 1. Lists S3 objects
        # 2. Gets each file's content
        # 3. Parses the recipe name
        # 4. Gets the creation date
        # 5. Returns a sorted list of dictionaries
        #
        # Your original /api/recipes route was re-doing this work and failing.
        # The correct code is to just call your function and return its result.
        
        recipe_list = storage.list_recipes(current_user.id)  # <--- Pass user_id
        return jsonify(recipe_list)

    except Exception as e:
        print(f"Recipe listing failed: {str(e)}")
        traceback.print_exc() # Add this for better debugging
        return jsonify({'error': str(e)}), 500

@app.route('/api/recipe/save', methods=['POST'])
@login_required
def save_recipe():
    try:
        data = request.get_json()
        filename = data.get('filename', '').strip()
        content = data.get('content', '').strip()
        user_id = current_user.id
        
        if not filename or not content:
            return jsonify({'error': 'Filename and content are required'}), 400
        
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        # Parse the recipe name from the content
        recipe_name = "Unknown Recipe"
        if content.startswith('# '):
            recipe_name = content.split('\n')[0][2:].strip()

        # Call save_recipe ONCE with all correct arguments
        if not storage.save_recipe(filename, content, recipe_name, user_id):
            return jsonify({'error': 'Failed to save recipe to S3'}), 500
        
        return jsonify({
            'success': True,
            'message': 'Recipe saved successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    

@app.route('/api/recipe/<filename>')
@login_required
def get_recipe_content(filename):
    try:
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        # Call get_recipe ONCE with the user_id
        content = storage.get_recipe(filename, current_user.id) 
        
        if content is None:
            return jsonify({'error': 'Recipe not found'}), 404
        
        return jsonify({'content': content})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recipe/<filename>', methods=['DELETE'])
@login_required  # <--- FIX: Add login_required
def delete_recipe(filename):
    try:
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        if not storage.delete_recipe(filename, current_user.id): # <--- Pass user_id
            return jsonify({'error': 'Failed to delete recipe from S3'}), 500
        
        return jsonify({
            'success': True,
            'message': 'Recipe deleted successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    

@app.route('/api/ocr', methods=['POST'])
@login_required
def process_ocr_text():
    try:
        data = request.get_json()
        user_id = current_user.id
        ocr_text = data.get('text', '').strip()

        if not ocr_text:
            return jsonify({'error': 'OCR text is required'}), 400

        # ... (all your OCR parsing logic remains the same) ...
        print("OCR Extracted Text:", ocr_text)
        cleaned_text = re.sub(r'\s+', ' ', ocr_text)
        cleaned_text = cleaned_text.replace('\n', '. ').strip()

        scraped_data = {
            'url': 'Photo Upload',
            'title': 'Recipe from Photo',
            'content': cleaned_text,
            'type': 'photo_ocr',
            'scraped_at': datetime.now().isoformat()
        }

        ai_response = scraper.parse_with_ai(scraped_data)

        if ai_response.strip() == "NO_RECIPE_FOUND":
            if "ingredient" in cleaned_text.lower() or "step" in cleaned_text.lower():
                ai_response = cleaned_text
            else:
                return jsonify({
                    'error': 'Could not extract a clear recipe from the image text.'
                }), 400

        markdown_content = scraper.create_markdown(ai_response, scraped_data)

        # 🗂️ Save to S3
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"recipe_photo_{timestamp}.md"

        # 🏷️ Extract recipe name from markdown
        recipe_name = "Photo Recipe"
        if markdown_content.startswith('# '):
            recipe_name = markdown_content.split('\n')[0][2:].strip()
            
        # Call save_recipe ONCE with all correct arguments
        if not storage.save_recipe(filename, markdown_content, recipe_name, user_id):
            return jsonify({'error': 'Failed to save recipe to S3'}), 500

        return jsonify({
            'success': True,
            'filename': filename,
            'recipe_name': recipe_name,
            'created': datetime.now().isoformat()
        })
    
    except Exception as e:
        traceback.print_exc() # Add this for better debugging
        return jsonify({'error': str(e)}), 500



@app.route('/api/scrape', methods=['POST'])
@login_required 
def scrape_recipe():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()

        # Validate URL
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        # --- LOGGED-IN USER ---
        # User is logged in, so we scrape AND save to their account
        result = scraper.scrape_and_save(url, current_user.id)

        if not result or result.get("status") == "failed":
            return jsonify({'error': result.get("error", "Unknown scraping error.")}), 400
        
        # Return success response (it's saved)
        return jsonify(result), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': f'Internal error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
