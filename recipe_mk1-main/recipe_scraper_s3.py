#!/usr/bin/env python3

import os
import json
import boto3
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse
import openai
import yt_dlp
from dotenv import load_dotenv
from botocore.exceptions import ClientError, NoCredentialsError

load_dotenv()

app = Flask(__name__)
CORS(app)

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
    
    def save_recipe(self, filename, content):
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{filename}",
                Body=content.encode('utf-8'),
                ContentType='text/markdown',
                Metadata={
                    'created': datetime.now().isoformat(),
                    'type': 'recipe'
                }
            )
            return True
        except ClientError:
            return False
    
    def get_recipe(self, filename):
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{filename}"
            )
            return response['Body'].read().decode('utf-8')
        except ClientError:
            return None
    
    def list_recipes(self):
        try:
            response = self.s3_client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix="recipes/recipe_"
            )
            
            recipes = []
            for obj in response.get('Contents', []):
                filename = obj['Key'].replace('recipes/', '')
                if filename.endswith('.md'):
                    try:
                        content = self.get_recipe(filename)
                        if content:
                            recipe_name = "Unknown Recipe"
                            if content.startswith('# '):
                                recipe_name = content.split('\n')[0][2:].strip()
                            
                            recipes.append({
                                'filename': filename,
                                'name': recipe_name,
                                'created': obj['LastModified'].isoformat()
                            })
                    except:
                        continue
            
            return sorted(recipes, key=lambda x: x['created'], reverse=True)
        except ClientError:
            return []
    
    def delete_recipe(self, filename):
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=f"recipes/{filename}"
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
    
    def is_youtube_url(self, url):
        youtube_patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)',
            r'youtube\.com.*[?&]v=',
            r'youtu\.be/'
        ]
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in youtube_patterns)
    
    def extract_youtube_transcript(self, url):
        try:
            ydl_opts = {
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en', 'en-US', 'en-GB'],
                'skip_download': True,
                'no_warnings': True,
            }
            
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
        content_text = scraped_data['content']
        
        recipe_sections = scraped_data.get('recipe_sections', {})
        if recipe_sections.get('ingredients') or recipe_sections.get('instructions'):
            sections_text = f"\nPRE-EXTRACTED INGREDIENTS:\n{chr(10).join(recipe_sections.get('ingredients', []))}\n\nPRE-EXTRACTED INSTRUCTIONS:\n{chr(10).join(recipe_sections.get('instructions', []))}"
            content_text = sections_text + "\n\nFULL PAGE CONTENT:\n" + content_text
        
        if scraped_data.get('structured_data'):
            structured_info = json.dumps(scraped_data['structured_data'], indent=2)
            content_text = f"STRUCTURED DATA:\n{structured_info}\n\n{content_text}"
        
        video_context = 'This is a transcript from a YouTube cooking video.' if scraped_data.get('type') == 'youtube_video' else 'This is from a recipe webpage.'
        video_rules = '- For video transcripts: ignore "like and subscribe", introductions, and off-topic chat' if scraped_data.get('type') == 'youtube_video' else ''
        
        if scraped_data.get('type') == 'photo_ocr':
            video_context = 'This is OCR text extracted from a photo of a recipe.'
            video_rules = '- For OCR text: ignore any misread characters, focus on extracting the recipe content'
        
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

URL: {scraped_data['url']}

Content:
{content_text}"""
        
        try:
            response = self.ai_client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
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
                max_tokens=2500,
                stream=False
            )
            ai_response = response.choices[0].message.content
            return ai_response
            
        except Exception:
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
    
    def scrape_and_save(self, url):
        scraped_data = self.scrape_url(url)
        if not scraped_data:
            raise Exception("Failed to scrape URL")
        
        ai_response = self.parse_with_ai(scraped_data)
        markdown_content = self.create_markdown(ai_response, scraped_data)
        
        domain = urlparse(url).netloc.replace('www.', '')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"recipe_{domain}_{timestamp}.md"
        
        if not self.storage.save_recipe(filename, markdown_content):
            raise Exception("Failed to save recipe to S3")
        
        recipe_name = "Unknown Recipe"
        if markdown_content.startswith('# '):
            recipe_name = markdown_content.split('\n')[0][2:].strip()
        
        return {
            'filename': filename,
            'recipe_name': recipe_name,
            'url': url,
            'content': markdown_content,
            'created': datetime.now().isoformat()
        }

try:
    storage = S3Storage()
    scraper = RecipeScraper(storage)
except ValueError as e:
    print(f"Configuration error: {e}")
    exit(1)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Recipe Manager</title>
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
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Recipe Manager</h1>
            <p>Manage your scraped recipes with cloud storage</p>
        </div>

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
                <span id="scrapeText">Scrape Recipe</span>
            </button>

            <div class="progress-bar" id="progressBar" style="display: none;">
                <div class="progress-fill" id="progressFill"></div>
            </div>
        </div>

        <div class="add-recipe-section">
            <h2 class="section-title">Add Recipe from Photo</h2>
            <p class="section-description">Upload a photo of a recipe or take a picture with your camera</p>

            <div class="input-group">
                <input 
                    type="file" 
                    class="input" 
                    id="imageInput" 
                    accept="image/*"
                    capture="environment"
                    style="display: none;"
                >
                <button class="btn btn-secondary" onclick="document.getElementById('imageInput').click()" style="width: 100%; margin-bottom: 1rem;">
                    Choose Photo
                </button>
                
                <div id="imagePreview" style="display: none; margin-bottom: 1rem;">
                    <img id="previewImg" style="max-width: 100%; max-height: 300px; border-radius: 8px; border: 1px solid #404040;">
                </div>
                
                <textarea 
                    id="ocrText" 
                    class="textarea" 
                    placeholder="OCR text will appear here. You can edit it before processing..."
                    style="min-height: 200px; display: none;"
                ></textarea>
            </div>

            <button class="btn" id="processOcrBtn" onclick="processOcrRecipe()" style="display: none;">
                <span id="processOcrText">Extract Recipe</span>
            </button>

            <div class="progress-bar" id="ocrProgressBar" style="display: none;">
                <div class="progress-fill" id="ocrProgressFill"></div>
            </div>
        </div>

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
            Refresh List
        </button>
    </div>

    <div id="editModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 class="modal-title">Edit Recipe</h2>
                <button class="close-btn" onclick="closeEditModal()">&times;</button>
            </div>
            
            <div class="input-group">
                <textarea id="editContent" class="textarea" placeholder="Edit your recipe in Markdown format..."></textarea>
            </div>
            
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeEditModal()">Cancel</button>
                <button class="btn btn-success" onclick="saveRecipe()">Save Changes</button>
            </div>
        </div>
    </div>

    <div id="deleteModal" class="modal">
        <div class="confirm-dialog">
            <h3>Delete Recipe</h3>
            <p>Are you sure you want to delete this recipe? This action cannot be undone.</p>
            <div class="confirm-actions">
                <button class="btn btn-secondary" onclick="closeDeleteModal()">Cancel</button>
                <button class="btn btn-danger" onclick="confirmDelete()">Delete</button>
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
                                Edit
                            </button>
                            <button class="btn btn-small btn-danger" onclick="deleteRecipe('${recipe.filename}', event)" title="Delete Recipe">
                                Delete
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

            if (expandedRecipe) {
                loadRecipeContent(expandedRecipe.filename);
            }
        }

        async function toggleRecipe(filename) {
            if (expandedRecipe?.filename === filename) {
                expandedRecipe = null;
            } else {
                expandedRecipe = recipes.find(r => r.filename === filename);
            }
            
            renderRecipeList();
        }

        async function loadRecipeContent(filename) {
            try {
                const content = await getRecipeContent(filename);
                if (content) {
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
                
                if (expandedRecipe?.filename === editingFilename) {
                    renderRecipeList();
                }
                
                const saveBtn = document.querySelector('.btn-success');
                const originalText = saveBtn.textContent;
                saveBtn.textContent = 'Saved!';
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
            scrapeText.textContent = 'Scraping...';
            progressBar.style.display = 'block';
            
            let progress = 0;
            const progressInterval = setInterval(() => {
                progress += Math.random() * 15;
                if (progress > 90) progress = 90;
                progressFill.style.width = progress + '%';
            }, 300);

            try {
                const result = await scrapeRecipeFromUrl(url);
                
                clearInterval(progressInterval);
                progressFill.style.width = '100%';
                
                await loadRecipeList();
                
                document.getElementById('urlInput').value = '';
                
                scrapeText.textContent = 'Recipe Added!';
                setTimeout(() => {
                    scrapeText.textContent = 'Scrape Recipe';
                }, 2000);
                
            } catch (error) {
                clearInterval(progressInterval);
                scrapeText.textContent = 'Failed to Scrape';
                setTimeout(() => {
                    scrapeText.textContent = 'Scrape Recipe';
                }, 2000);
                console.error('Scraping failed:', error);
                alert('Failed to scrape recipe: ' + error.message);
            } finally {
                scrapeBtn.disabled = false;
                setTimeout(() => {
                    progressBar.style.display = 'none';
                    progressFill.style.width = '0%';
                }, 1000);
            }
        }

        document.getElementById('imageInput').addEventListener('change', function(e) {
            const file = e.target.files[0];
            if (file && file.type.startsWith('image/')) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    const previewImg = document.getElementById('previewImg');
                    const imagePreview = document.getElementById('imagePreview');
                    previewImg.src = e.target.result;
                    imagePreview.style.display = 'block';
                    
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
                ocrProgressBar.style.display = 'block';
                ocrText.value = 'Processing image...';
                ocrText.style.display = 'block';
                
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
                
                ocrProgressFill.style.width = '100%';
                
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

            processBtn.disabled = true;
            processText.textContent = 'Extracting Recipe...';
            progressBar.style.display = 'block';
            
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
                
                clearInterval(progressInterval);
                progressFill.style.width = '100%';
                
                await loadRecipeList();
                
                document.getElementById('imageInput').value = '';
                document.getElementById('ocrText').value = '';
                document.getElementById('imagePreview').style.display = 'none';
                document.getElementById('ocrText').style.display = 'none';
                document.getElementById('processOcrBtn').style.display = 'none';
                
                processText.textContent = 'Recipe Added!';
                setTimeout(() => {
                    processText.textContent = 'Extract Recipe';
                }, 2000);
                
            } catch (error) {
                clearInterval(progressInterval);
                processText.textContent = 'Failed to Extract';
                setTimeout(() => {
                    processText.textContent = 'Extract Recipe';
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

        document.getElementById('urlInput').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                scrapeRecipe();
            }
        });

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

        loadRecipeList();
    </script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/recipes')
def get_recipes():
    try:
        recipes = storage.list_recipes()
        return jsonify(recipes)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/recipe/save', methods=['POST'])
def save_recipe():
    try:
        data = request.get_json()
        filename = data.get('filename', '').strip()
        content = data.get('content', '').strip()
        
        if not filename or not content:
            return jsonify({'error': 'Filename and content are required'}), 400
        
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        if not storage.save_recipe(filename, content):
            return jsonify({'error': 'Failed to save recipe to S3'}), 500
        
        return jsonify({
            'success': True,
            'message': 'Recipe saved successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/recipe/<filename>')
def get_recipe_content(filename):
    try:
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        content = storage.get_recipe(filename)
        if content is None:
            return jsonify({'error': 'Recipe not found'}), 404
        
        return jsonify({'content': content})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/recipe/<filename>', methods=['DELETE'])
def delete_recipe(filename):
    try:
        if not filename.startswith('recipe_') or not filename.endswith('.md'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        if not storage.delete_recipe(filename):
            return jsonify({'error': 'Failed to delete recipe from S3'}), 500
        
        return jsonify({
            'success': True,
            'message': 'Recipe deleted successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ocr', methods=['POST'])
def process_ocr_text():
    try:
        data = request.get_json()
        ocr_text = data.get('text', '').strip()
        
        if not ocr_text:
            return jsonify({'error': 'OCR text is required'}), 400
        
        scraped_data = {
            'url': 'Photo Upload',
            'title': 'Recipe from Photo',
            'content': ocr_text,
            'type': 'photo_ocr',
            'scraped_at': datetime.now().isoformat()
        }
        
        ai_response = scraper.parse_with_ai(scraped_data)
        
        if ai_response.strip() == "NO_RECIPE_FOUND":
            return jsonify({'error': 'Could not extract a clear recipe from the image text. Please try a clearer photo or check if the image contains a recipe.'}), 400
        
        markdown_content = scraper.create_markdown(ai_response, scraped_data)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"recipe_photo_{timestamp}.md"
        
        if not storage.save_recipe(filename, markdown_content):
            return jsonify({'error': 'Failed to save recipe to S3'}), 500
        
        recipe_name = "Photo Recipe"
        if markdown_content.startswith('# '):
            recipe_name = markdown_content.split('\n')[0][2:].strip()
        
        return jsonify({
            'success': True,
            'filename': filename,
            'recipe_name': recipe_name,
            'created': datetime.now().isoformat()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/scrape', methods=['POST'])
def scrape_recipe():
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        result = scraper.scrape_and_save(url)
        
        return jsonify({
            'success': True,
            'filename': result['filename'],
            'recipe_name': result['recipe_name'],
            'url': result['url'],
            'created': result['created']
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)