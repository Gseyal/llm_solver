import google.generativeai as genai

genai.configure(api_key="AIzaSyCB4wMuwl2RHKCqWMNxUWOHRnDDyer9B7Y")

models = genai.list_models()
for m in models:
    print(m.name, " |  supports generate_content:", "generateContent" in m.supported_generation_methods)