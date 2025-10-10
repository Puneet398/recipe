#!/usr/bin/env python3

import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime
import os
from urllib.parse import urlparse
import sys
import openai
import yt_dlp

class RecipeScraper:
    def __init__(self):
        # Replace with your actual Groq API key
        api_key = os.getenv('GROQ_API_KEY') or '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'
        
        self.ai_client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1"
        )
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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
            print(f"📺 Processing YouTube video: {url}")
            
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
                    print("⚠️  No transcript found, trying description...")
                    transcript_text = info.get('description', '')
                
                video_data = {
                    "url": url,
                    "title": title,
                    "duration": duration,
                    "content": transcript_text,
                    "type": "youtube_video",
                    "scraped_at": datetime.now().isoformat()
                }
                
                print("✅ YouTube transcript extracted successfully")
                return video_data
                
        except Exception as e:
            print(f"❌ Error extracting YouTube transcript: {str(e)}")
            print("💡 Tip: Make sure the video has captions/subtitles enabled")
            return None
    
    def parse_vtt_content(self, vtt_content):
        lines = vtt_content.split('\n')
        text_lines = []
        
        for line in lines:
            line = line.strip()
            # Skip empty lines, WEBVTT headers, NOTE lines, timestamp lines, and numbered cue identifiers
            if (not line or 
                line.startswith('WEBVTT') or 
                line.startswith('NOTE') or
                '-->' in line or
                re.match(r'^\d+$', line)):
                continue
            
            # Remove HTML tags and HTML entities
            line = re.sub(r'<[^>]+>', '', line)
            line = re.sub(r'&\w+;', '', line)
            
            if line:
                text_lines.append(line)
        
        return ' '.join(text_lines)
    
    def extract_recipe_sections(self, content):
        """Pre-process content to identify and extract recipe sections more clearly"""
        lines = content.split('\n')
        
        # Find ingredients section
        ingredients_section = []
        instructions_section = []
        
        in_ingredients = False
        in_instructions = False
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Detect ingredients section start
            if re.search(r'\bingredients?\b', line, re.IGNORECASE) and len(line) < 100:
                in_ingredients = True
                in_instructions = False
                continue
            
            # Detect instructions/method section start
            if re.search(r'\b(instructions?|method|directions?|steps?)\b', line, re.IGNORECASE) and len(line) < 100:
                in_instructions = True
                in_ingredients = False
                continue
            
            # Collect ingredients
            if in_ingredients:
                # Stop if we hit a new section
                if re.search(r'\b(method|instructions?|directions?|steps?|nutrition|notes)\b', line, re.IGNORECASE):
                    in_ingredients = False
                    in_instructions = 'instructions' in line.lower() or 'method' in line.lower()
                    continue
                
                if line and not line.startswith(('▢', '•', '-', '*')):
                    # Look for ingredient-like patterns
                    if any(indicator in line.lower() for indicator in ['g ', 'ml', 'tbsp', 'tsp', 'cup', 'oz', 'lb', 'clove', 'onion', 'garlic']):
                        ingredients_section.append(line)
                elif line.startswith(('▢', '•', '-', '*')):
                    ingredients_section.append(line[1:].strip())
            
            # Collect instructions
            if in_instructions:
                # Stop if we hit nutrition or notes
                if re.search(r'\b(nutrition|notes|tips|faq)\b', line, re.IGNORECASE):
                    in_instructions = False
                    continue
                
                if line:
                    # Look for step patterns
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
            print(f"📄 Fetching webpage: {url}")
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Remove only the most disruptive elements, keep more content
            for element in soup(["script", "style", "nav", "header", "footer"]):
                element.decompose()
            
            # Extract structured recipe data (JSON-LD)
            structured_recipe = self.extract_structured_data(soup)
            
            # Get page title
            title = soup.find('title')
            page_title = title.get_text().strip() if title else ""
            
            # Get ALL text content - let AI filter what's important
            text_content = soup.get_text()
            
            # Clean up text content but keep more of it
            lines = (line.strip() for line in text_content.splitlines())
            text_content = '\n'.join(line for line in lines if line)
            
            # Extract recipe sections for better parsing
            recipe_sections = self.extract_recipe_sections(text_content)
            
            scraped_data = {
                "url": url,
                "title": page_title,
                "content": text_content[:15000],  # Increased content limit
                "structured_data": structured_recipe,
                "recipe_sections": recipe_sections,
                "scraped_at": datetime.now().isoformat()
            }
            
            print("✅ Webpage scraped successfully")
            print(f"📊 Content length: {len(text_content)} chars (keeping {min(len(text_content), 15000)})")
            return scraped_data
            
        except Exception as e:
            print(f"❌ Error scraping URL: {str(e)}")
            return None
    
    def extract_structured_data(self, soup):
        """Extract JSON-LD structured data for recipes"""
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
    
    def save_scraped_data(self, data, filename=None):
        """Save scraped data to JSON file"""
        if not filename:
            domain = urlparse(data['url']).netloc.replace('www.', '')
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"scraped_{domain}_{timestamp}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"💾 Scraped data saved to: {filename}")
        return filename
    
    def parse_with_ai(self, scraped_data):
        """Parse scraped content using Groq AI with improved prompting"""
        print("🤖 Analyzing content with AI...")
        
        # Use the full text content for AI analysis
        content_text = scraped_data['content']
        
        # Include extracted recipe sections if available
        recipe_sections = scraped_data.get('recipe_sections', {})
        if recipe_sections.get('ingredients') or recipe_sections.get('instructions'):
            sections_text = f"\nPRE-EXTRACTED INGREDIENTS:\n{chr(10).join(recipe_sections.get('ingredients', []))}\n\nPRE-EXTRACTED INSTRUCTIONS:\n{chr(10).join(recipe_sections.get('instructions', []))}"
            content_text = sections_text + "\n\nFULL PAGE CONTENT:\n" + content_text
        
        # Also include structured data if available
        if scraped_data.get('structured_data'):
            structured_info = json.dumps(scraped_data['structured_data'], indent=2)
            content_text = f"STRUCTURED DATA:\n{structured_info}\n\n{content_text}"
        
        video_context = 'This is a transcript from a YouTube cooking video.' if scraped_data.get('type') == 'youtube_video' else 'This is from a recipe webpage.'
        video_rules = '- For video transcripts: ignore "like and subscribe", introductions, and off-topic chat' if scraped_data.get('type') == 'youtube_video' else ''
        
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
                max_tokens=2500,  # Increased to allow for more detailed steps
                stream=False
            )
            ai_response = response.choices[0].message.content
            
            # Post-process to ensure completeness
            ai_response = self.validate_recipe_completeness(ai_response, scraped_data)
            
            print("✅ Groq analysis complete")
            return ai_response
            
        except Exception as e:
            print(f"❌ Groq parsing failed: {str(e)}")
            print("💡 Make sure GROQ_API_KEY is set correctly")
            return self.fallback_parse(scraped_data)
    
    def validate_recipe_completeness(self, ai_response, scraped_data):
        """Validate that the AI response includes all essential steps"""
        if ai_response.strip() == "NO_RECIPE_FOUND":
            return ai_response
        
        # Check if structured data has more steps than AI response
        structured_data = scraped_data.get('structured_data', {})
        if structured_data and 'recipeInstructions' in structured_data:
            structured_steps = structured_data['recipeInstructions']
            
            # Count steps in AI response
            ai_steps = re.findall(r'^\d+\.', ai_response, re.MULTILINE)
            
            # If structured data has significantly more steps, add a note
            if len(structured_steps) > len(ai_steps) + 2:
                print(f"⚠️  Warning: Original recipe had {len(structured_steps)} steps, AI extracted {len(ai_steps)}")
                print("💡 Consider checking the output for completeness")
        
        return ai_response
    
    def fallback_parse(self, scraped_data):
        """Improved fallback parsing when AI fails"""
        print("🔄 Using fallback parsing...")
        
        content = scraped_data['content']
        structured = scraped_data.get('structured_data') or {}
        recipe_sections = scraped_data.get('recipe_sections', {})
        
        # Extract title
        title = structured.get('name') or scraped_data['title'].split('|')[0].strip()
        
        # Extract ingredients - prefer structured data, then pre-extracted sections
        ingredients = structured.get('recipeIngredient', [])
        if not ingredients and recipe_sections.get('ingredients'):
            ingredients = recipe_sections['ingredients']
        
        if not ingredients:
            # Look for ingredients in the content using patterns
            lines = content.split('\n')
            
            # Find ingredients section and extract ingredients
            in_ingredients_section = False
            for line in lines:
                line = line.strip()
                if re.search(r'\bingredients?\b', line, re.IGNORECASE) and len(line) < 50:
                    in_ingredients_section = True
                    continue
                elif in_ingredients_section and re.search(r'\b(method|instructions?|directions?)\b', line, re.IGNORECASE):
                    break
                elif in_ingredients_section and line:
                    # Check if line looks like an ingredient
                    if any(unit in line.lower() for unit in ['tbsp', 'tsp', 'cup', 'ml', 'g', 'kg', 'lb', 'oz', 'clove', 'small', 'large', 'medium']):
                        ingredients.append(line)
        
        # Extract instructions - prefer structured data, then pre-extracted sections
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
        
        if not instructions:
            # Look for method steps in content
            lines = content.split('\n')
            current_step = ""
            
            for line in lines:
                line = line.strip()
                # Look for numbered steps or cooking action words
                if re.match(r'^step \d+', line.lower()) or re.match(r'^\d+\.?\s+', line):
                    if current_step:
                        instructions.append(current_step)
                    # Clean up the step
                    current_step = re.sub(r'^step \d+', '', line, flags=re.IGNORECASE).strip()
                    current_step = re.sub(r'^\d+\.?\s*', '', current_step).strip()
                elif current_step and line and not re.match(r'^\d+\.?\s+', line):
                    # Continue previous step if it's a continuation
                    current_step += " " + line
                elif not current_step and any(action in line.lower() for action in ['cook', 'add', 'heat', 'stir', 'mix', 'drain', 'serve']):
                    current_step = line
            
            if current_step:
                instructions.append(current_step)
        
        if not ingredients and not instructions:
            return "NO_RECIPE_FOUND"
        
        # Format the output
        formatted_ingredients = '\n'.join('• ' + ing for ing in ingredients[:20]) if ingredients else '• No ingredients found'
        formatted_instructions = '\n'.join(f'{i+1}. {inst}' for i, inst in enumerate(instructions[:15])) if instructions else '1. No instructions found'
        
        return f"""# {title}

**Ingredients:**
{formatted_ingredients}

**Method:**
{formatted_instructions}"""
    
    def create_markdown(self, ai_response, scraped_data):
        """Create final markdown output"""
        if ai_response.strip() == "NO_RECIPE_FOUND":
            return f"""# No Recipe Found

**URL:** {scraped_data['url']}

Could not extract a clear recipe from this URL. The page may not contain a recipe or may be behind a paywall."""
        
        # Add URL if not already present
        if scraped_data['url'] not in ai_response:
            lines = ai_response.split('\n')
            if lines and not ai_response.strip() == "NO_RECIPE_FOUND":
                title_line = lines[0]
                rest = '\n'.join(lines[1:]) if len(lines) > 1 else ''
                ai_response = f"{title_line}\n\n**URL:** {scraped_data['url']}\n\n{rest}"
        
        return ai_response
    
    def run(self):
        """Main execution method"""
        print("🍳 Recipe Scraper with Groq AI")
        print("📺 Now supports YouTube videos!")
        print("🔍 Enhanced step extraction!")
        print("=" * 40)
        
        url = input("Enter recipe URL (website or YouTube): ").strip()
        if not url:
            print("❌ No URL provided")
            return
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Scrape the URL
        scraped_data = self.scrape_url(url)
        if not scraped_data:
            print("❌ Failed to scrape URL")
            return
        
        # Save raw scraped data
        json_filename = self.save_scraped_data(scraped_data)
        
        # Parse with AI
        ai_response = self.parse_with_ai(scraped_data)
        
        # Create final markdown
        markdown_content = self.create_markdown(ai_response, scraped_data)
        
        # Save markdown file
        domain = urlparse(url).netloc.replace('www.', '')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        markdown_filename = f"recipe_{domain}_{timestamp}.md"
        
        with open(markdown_filename, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        print(f"📝 Recipe saved to: {markdown_filename}")
        print("\n" + "=" * 40)
        print("RECIPE OUTPUT:")
        print("=" * 40)
        print(markdown_content)

def main():
    """Main entry point"""
    try:
        scraper = RecipeScraper()
        scraper.run()
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}")

if __name__ == "__main__":
    main()