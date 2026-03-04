import json
import os

def cleanup_catalog(input_file="enriched_denver_catalog.json", output_file="enriched_denver_catalog_cleaned.json"):
    if not os.path.exists(input_file):
        print(f"❌ Error: {input_file} not found!")
        return

    with open(input_file, "r") as f:
        data = json.load(f)

    initial_count = len(data)
    
    # 🔍 Filter: Keep only items that DO NOT start with 'survey123_'
    # We use .lower() just in case there is inconsistent casing
    cleaned_data = [
        item for item in data 
        if not item.get('service_name', '').lower().startswith('survey123_')
    ]
    
    removed_count = initial_count - len(cleaned_data)

    with open(output_file, "w") as f:
        json.dump(cleaned_data, f, indent=4)

    print(f"✅ Cleanup Complete!")
    print(f"📊 Total processed: {initial_count}")
    print(f"🗑️ Surveys removed: {removed_count}")
    print(f"💾 Cleaned file saved as: {output_file}")

if __name__ == "__main__":
    cleanup_catalog()