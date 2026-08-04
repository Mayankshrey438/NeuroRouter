# NeuroRouter
It's an LLM Gateway — a backend service that sits between your app and AI providers like Groq or OpenAI. Instead of calling an LLM directly, your app calls NeuroRouter, which decides which model to use, caches repeat questions, and reroutes traffic automatically if a provider goes down. 
