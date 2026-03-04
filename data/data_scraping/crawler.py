import requests
import json
import time
from bs4 import BeautifulSoup
import re

ROOT_URL = "https://services1.arcgis.com/zdB7qR0BtYrg0Xpl/arcgis/rest/services?f=json"

# Use a Session for better performance
session = requests.Session()

def clean_html(text: str) -> str:
    """Removes HTML tags and cleans up whitespace/entities."""
    if not text:
        return ""
    
    # 1. Parse with BeautifulSoup to handle tags and entities
    soup = BeautifulSoup(text, "html.parser")
    clean_text = soup.get_text(separator=" ") # Use space as separator for <br> tags
    
    # 2. Remove multiple spaces and newlines left over
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    
    return clean_text

def get_layer_fields(url: str):
    """Fetches and cleans field names from a specific layer."""
    try:
        # Note: We removed 'async' because requests is synchronous
        resp = session.get(url, params={"f": "json"}, timeout=10)
        data = resp.json()
        layer_fields = data.get("fields", [])
        # We capture Name and Alias for the LLM's benefit
        return [{"name": f['name'], "alias": f.get('alias', f['name'])} for f in layer_fields]
    except Exception as e:
        print(f"❌ Error parsing fields at {url}: {e}")
        return []

def crawl_denver():
    print("🚀 Starting Denver Open Data Crawl...")
    try:
        services = session.get(ROOT_URL).json().get("services", [])
    except Exception as e:
        print(f"Critical Error: Could not reach Root URL. {e}")
        return []

    catalog = []

    for s in services:
        name = s['name']
        service_url = s["url"]
        
        try:
            # 1. Get the Service-level metadata
            meta = session.get(service_url, params={"f": "json"}, timeout=10).json()
            
            # 2. Combine descriptions
            desc = clean_html(meta.get("description") or "")
            svc_desc = clean_html(meta.get("serviceDescription") or "")
            if svc_desc.lower() in desc.lower():
                full_description = desc
            elif desc.lower() in svc_desc.lower():
                full_description = svc_desc
            else:
                full_description = f"{svc_desc} {desc}".strip()
            
            # 3. Iterate through child layers
            layers_in_meta = meta.get("layers", [])
            processed_layers = []
            
            for layer in layers_in_meta:
                layer_id = layer["id"]
                layer_name = layer["name"]
                # Navigate to the specific layer URL to get fields
                layer_url = f"{service_url}/{layer_id}"
                
                fields = get_layer_fields(layer_url)
                
                processed_layers.append({
                    "id": layer_id,
                    "name": layer_name,
                    "fields": fields,
                })

            catalog.append({
                "service_name": name,
                "base_url": service_url,
                "description": full_description,
                "layers": processed_layers
            })

            print(f"✅ Indexed Service: {name} ({len(processed_layers)} layers)")
            # Polite scraping: tiny sleep to avoid rate limiting
            time.sleep(0.1) 
            
        except Exception as e:
            print(f"❌ Failed to parse service {name}: {e}")
            
    return catalog

# Run and Save
full_data = crawl_denver()
with open("raw_denver_metadata.json", "w") as f:
    json.dump(full_data, f, indent=4)
print(f"🎉 Success! Created raw_denver_metadata.json with {len(full_data)} services.")