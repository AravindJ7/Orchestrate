import sys
from openai import OpenAI

# 1. Initialize the client using OpenRouter's URL
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("api_key"),  # <-- Put your sk-or-... key here
)

# 2. Pick your model (You can change this to any model on OpenRouter)
MODEL_NAME = "openrouter/free" 

# 3. Initialize the conversation history list
messages = [
    {"role": "system", "content": "You are a helpful, direct, and concise AI assistant."}
]

print("====================================================")
print(f"Interactive Chat Started (Using model: {MODEL_NAME})")
print("Type 'exit' or 'quit' to end the chat.")
print("====================================================\n")

# 4. Continuous conversation loop
while True:
    try:
        # Get input from the user
        user_input = input("You: ")
        
        # Check for exit command
        if user_input.strip().lower() in ['exit', 'quit']:
            print("\nGoodbye!")
            break
            
        # Skip empty inputs
        if not user_input.strip():
            continue
            
        # Add user's message to history
        messages.append({"role": "user", "content": user_input})
        
        print("AI is thinking...", end="\r")
        
        # Call the API with the entire message history so it remembers past context
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages
        )
        
        # Extract the assistant's reply
        ai_reply = response.choices[0].message.content
        
        # Clear the "thinking..." line and print the response
        print(" " * 20, end="\r") 
        print(f"AI: {ai_reply}\n")
        
        # Add assistant's reply to history so it retains context for the next turn
        messages.append({"role": "assistant", "content": ai_reply})

    except KeyboardInterrupt:
        print("\n\nChat interrupted. Goodbye!")
        sys.exit(0)
    except Exception as e:
        print(f"\nAn error occurred: {e}\n")
