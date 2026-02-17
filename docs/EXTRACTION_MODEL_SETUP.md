# Memory Extraction Model Setup Guide

> **For AI Assistants:** If you're modifying extraction behavior, read [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) first!

## Overview

The LangGraph Memory Filter uses OpenWebUI's internal model system to extract user information from conversations. This guide shows you how to configure the extraction model for optimal results.

**Key Feature (Schema v2):** Preferences are tracked as timestamped data points for evolution tracking. The same preference extracted multiple times creates multiple entries to show how opinions change over time.

**PII Protection:** The filter includes 3-layer PII filtering. Even if the extraction model returns PII, it will be caught and removed/redacted by the post-extraction validator. The extraction prompt should include PII policy instructions — see `prompt/EXTRACTION_PROMPT.md` for the reference prompt with PII policy.

---

## Extraction Model Options

You have **two approaches** for configuring the extraction model:

### **Option 1: Use Existing Model (Quick Start)**

Simply specify an existing model ID in the filter valves:

```yaml
extraction_model_id: "llama3.2:latest"
# or
extraction_model_id: "gpt-4o-mini"
# or any model you have configured in OpenWebUI
```

**Pros:**
- ✅ No setup required
- ✅ Works immediately

**Cons:**
- ❌ May produce inconsistent JSON
- ❌ Extraction quality depends on base model
- ❌ Requires good model (Llama 3.2+, GPT-4, etc.)

---

### **Option 2: Create Custom Extraction Model (Recommended)**

Create a dedicated model in OpenWebUI with a system prompt optimized for memory extraction. This gives you **maximum control** over extraction behavior without changing code.

#### **Step 1: Create Model in Admin Panel**

1. Navigate to **Admin Panel** → **Settings** → **Models**
2. Click **"+ Add Model"**
3. Configure:
   - **Name:** `Memory Extractor`
   - **Model ID:** `memory-extractor` (or any unique ID)
   - **Base Model:** Choose a good JSON model (e.g., `llama3.2:latest`, `gpt-4o-mini`)
   - **System Prompt:** See below

#### **Step 2: System Prompt for Extraction**

```
You are a memory extraction specialist. Your ONLY job is to analyze conversations and extract personal information about the user in strict JSON format.

CRITICAL RULES:
1. Extract ONLY information explicitly stated by the USER (role: "user"), NOT the assistant
2. Return ONLY valid JSON - no markdown, no explanations, no code blocks
3. Use empty strings/arrays for missing information - never use placeholders
4. Be conservative - only extract facts you're certain about

OUTPUT FORMAT:
{
    "personal_info": {
        "name": "",
        "age": null,
        "location": "",
        "occupation": "",
        "company": ""
    },
    "relationships": [
        {
            "entity_name": "person's name",
            "relationship_type": "friend|family|colleague|partner|etc",
            "details": "optional context"
        }
    ],
    "preferences": [
        {
            "category": "food|color|activity|music|etc",
            "value": "the specific thing",
            "sentiment": "like|dislike|neutral|love|hate",
            "confidence": 0.8
        }
    ],
    "important_dates": [
        {
            "date_type": "birthday|anniversary|etc",
            "date_value": "date or natural language",
            "entity": "whose date (optional)"
        }
    ],
    "goals": [
        {
            "goal_text": "what they want to achieve",
            "category": "career|personal|health|education|etc",
            "status": "active"
        }
    ],
    "interests": [
        {
            "interest_name": "hobby or interest",
            "proficiency": "beginner|intermediate|expert|null",
            "frequency": "daily|weekly|occasionally|null"
        }
    ]
}

EXAMPLES:

User: "My name is Alex and I work at Microsoft as a Senior Engineer"
Output:
{
    "personal_info": {"name": "Alex", "age": null, "location": "", "occupation": "Senior Engineer", "company": "Microsoft"},
    "relationships": [],
    "preferences": [],
    "important_dates": [],
    "goals": [],
    "interests": []
}

User: "My friend Sarah loves Italian food"
Output:
{
    "personal_info": {},
    "relationships": [{"entity_name": "Sarah", "relationship_type": "friend", "details": ""}],
    "preferences": [{"category": "food", "value": "Italian food", "sentiment": "love", "confidence": 0.9}],
    "important_dates": [],
    "goals": [],
    "interests": []
}

User: "I'm planning to learn Python this year. My favorite color is blue."
Output:
{
    "personal_info": {},
    "relationships": [],
    "preferences": [{"category": "color", "value": "blue", "sentiment": "like", "confidence": 1.0}],
    "important_dates": [],
    "goals": [{"goal_text": "learn Python", "category": "education", "status": "active"}],
    "interests": []
}

Remember: Return ONLY the JSON object. No markdown, no explanations.
```

#### **Step 3: Configure Filter**

1. Go to **Settings** → **Functions** → **LangGraph Memory Graph**
2. Set valve:
   ```yaml
   extraction_model_id: "memory-extractor"
   extraction_model_temperature: 0.1  # Low for consistent JSON
   extraction_model_max_tokens: 1000
   ```

---

## Model Requirements

### **Recommended Models for Extraction:**

| Model | Quality | Speed | Cost | Notes |
|-------|---------|-------|------|-------|
| **GPT-4o** | ⭐⭐⭐⭐⭐ | Fast | High | Best JSON reliability |
| **GPT-4o-mini** | ⭐⭐⭐⭐ | Very Fast | Low | Great balance |
| **Llama 3.2 3B** | ⭐⭐⭐ | Very Fast | Free | Good for simple extraction |
| **Llama 3.3 70B** | ⭐⭐⭐⭐⭐ | Medium | Free | Excellent if you have GPU |
| **Qwen 2.5** | ⭐⭐⭐⭐ | Fast | Free | Great JSON output |
| **Gemini Flash** | ⭐⭐⭐⭐ | Very Fast | Low | Fast and cheap |

### **Minimum Requirements:**
- Must support JSON output
- Should understand structured data
- Needs to follow system prompts reliably

### **Not Recommended:**
- ❌ Models < 3B parameters (too unreliable)
- ❌ Models without instruction tuning
- ❌ Pure completion models (need chat format)

---

## Testing Your Extraction Model

### **Test in Chat:**

Create a test conversation:

```
User: "Hi, I'm John. I work at Google as a Product Manager. My wife Sarah loves cooking."