import json
import google.generativeai as genai
import time
from pathlib import Path
import os

# 1. Setup Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-2.5-flash')

def generate_summary(service_data):
    """Sends a single service's metadata to Gemini for summarization."""
    
    # Flatten the layers/fields so it's easier for the LLM to read
    layers_context = ""
    
    # Junk fields to ignore to save tokens and improve focus
    ignore_list = ['objectid', 'shape', 'st_area', 'st_length', 'globalid', 'fid', "shape__area", "shape__length", "id", "uniqueid", "objectid_1"]

    for layer in service_data.get('layers', []):
        # 1. Filter out the junk fields
        all_fields = [f.get('alias', f['name']) for f in layer.get('fields', [])]
        meaningful_fields = [f for f in all_fields if f.lower() not in ignore_list]
        
        # 2. Use a larger limit (e.g., 50) since 2.0 has a huge context window
        field_sample = meaningful_fields[:50]
        
        layers_context += f"- Layer: {layer['name']} (ID: {layer['id']})\n"
        layers_context += f"  Key Attributes: {', '.join(field_sample)}\n"

    prompt = f"""
    You are a Data Librarian for the City of Denver. 
    Analyze the following ArcGIS metadata and write a concise 3-sentence summary for a search index.
    
    SERVICE NAME: {service_data['service_name']}
    TECHNICAL DESCRIPTION: {service_data['description']}
    LAYERS & ATTRIBUTES:
    {layers_context}
    
    TASK:
    Generate a search-optimized summary consisting of EXACTLY 3 sentences:

    1. Sentence 1 (Identity): Define the core topic and time period of the dataset (e.g., '2023 property zoning' or 'historical crime data').
    2. Sentence 2 (Attributes): Incorporate the most relevant field aliases, human-readable attributes and data points (e.g., 'inspection dates', 'species names') into a description of what the data tracks (based on layers and attributes).
    3. Sentence 3 (Utility): State a specific analytical question or user query this data is designed to answer including key data points a user might ask for (e.g., 'Which neighborhoods have the highest density of maple trees?').

    OUTPUT FORMAT: Return ONLY 3 sentences of plain text. No headers, no bullets, and no introductory filler.
    """

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ Error summarizing {service_data['service_name']}: {e}")
        return "Summary unavailable."
    
def is_high_quality(service):
    """Returns True if the service has enough data to be useful."""
    layers = service.get('layers', [])
    
    # 1. Guard: No layers = No data
    if not layers:
        return False, "No layers found"

    # 2. Guard: Total field count across all layers
    total_fields = sum(len(l.get('fields', [])) for l in layers)
    if total_fields == 0:
        return False, "Total fields are zero"

    # 3. Guard: Meaningless Descriptions
    # If the description is just 'None', 'NULL', or empty string
    desc = service.get('description', '').strip()
    if not desc and len(layers[0].get('name', '')) < 3:
        return False, "Insufficient context for enrichment"

    return True, "Passed"

def enrich_catalog():
    # Load your raw data
    with open("raw_denver_metadata.json", "r") as f:
        raw_data = json.load(f)

    enriched_data = []
    
    print(f"🚀 Enriching {len(raw_data)} services...")

    for i, service in enumerate(raw_data):
        print(f"[{i+1}/{len(raw_data)}] Summarizing: {service['service_name']}...")

        quality_passed, reason = is_high_quality(service)
    
        if not quality_passed:
            print(f"⏩ Skipping {service['service_name']}: {reason}")
            continue
        
        summary = generate_summary(service)
        
        # We store the summary at the TOP level for the Vector DB
        service['semantic_summary'] = summary
        enriched_data.append(service)
        
        # Avoid hitting rate limits (30-60 RPM is typical for free tier)
        time.sleep(0.2) 

        # Periodic Save
        if (i + 1) % 10 == 0:
            with open("enriched_denver_catalog_partial.json", "w") as f:
                json.dump(enriched_data, f, indent=4)

    # Final Save
    with open("enriched_denver_catalog.json", "w") as f:
        json.dump(enriched_data, f, indent=4)
    print("✅ Enrichment complete! Saved to enriched_denver_catalog.json")

if __name__ == "__main__":
    enrich_catalog()