import os
from PIL import Image
from io import BytesIO

from app.services.graph.builder import compiled_graph

def main():
    try:
        # Get PNG bytes from LangGraph
        print("Generating Mermaid diagram bytes...")
        png_bytes = compiled_graph.get_graph().draw_mermaid_png()
        
        # Convert PNG bytes to JPG using Pillow
        print("Converting to JPG...")
        image = Image.open(BytesIO(png_bytes))
        # Ensure it's in RGB mode before saving as JPG
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
            
        output_path = "/home/lambda/Downloads/langgraph_diagram.jpg"
        
        # Ensure Downloads directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        image.save(output_path, "JPEG", quality=95)
        print(f"Successfully saved LangGraph diagram to {output_path}")
    except Exception as e:
        print(f"Error generating diagram: {e}")

if __name__ == "__main__":
    main()
