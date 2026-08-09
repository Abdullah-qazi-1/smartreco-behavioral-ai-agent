## services/category_taxonomy.py
"""
Central source of truth for category names, topic keywords, and
inter-category relatedness. Used by:
  - seed_data.py (to generate course titles/descriptions per category)
  - services/interest_profile.py (to map onboarding interests onto real
    Product categories, spread score to related categories, and match
    search queries to categories)
  - services/retrieval.py (category-constrained search + search-intent
    cross-field bridging)

Moved here (instead of living inline in seed_data.py) so there's a
single source of truth — no duplicated/drifting category data.
"""
import re
from collections import defaultdict
from typing import Dict, List, Optional

# ---------------------------------------------------------------
# The 10 real Product categories, with topic keywords used both for
# seeding course titles AND for matching free-text search queries to
# a category (see infer_category_from_query below).
# ---------------------------------------------------------------
CATEGORY_TOPICS: Dict[str, List[str]] = {
    "Machine Learning": [
        "Linear & Logistic Regression", "Decision Trees & Random Forests", "Support Vector Machines",
        "Gradient Boosting (XGBoost/LightGBM)", "Feature Engineering", "Model Evaluation & Cross-Validation",
        "Unsupervised Learning & Clustering", "Dimensionality Reduction (PCA/t-SNE)", "Time Series Forecasting",
        "Anomaly Detection", "Recommender Systems", "Hyperparameter Tuning", "Ensemble Learning",
        "Bayesian Machine Learning", "Explainable AI (SHAP/LIME)", "ML Pipelines with scikit-learn",
        "Machine Learning Math Foundations",
    ],
    "AI Engineering": [
        "Prompt Engineering", "Retrieval-Augmented Generation (RAG)", "LangChain Application Development",
        "LangGraph Multi-Agent Systems", "Fine-Tuning LLMs with LoRA", "Vector Databases & Embeddings",
        "AI Agent Design Patterns", "LLM Evaluation & Observability", "Building Chatbots with LLMs",
        "Local LLMs with Ollama", "OpenAI API Integration", "Semantic Search Systems",
        "AI Cost Optimization", "Multimodal AI Applications", "Speech & Audio AI",
        "Computer Vision with Deep Learning", "Generative AI for Business",
    ],
    "Web Development": [
        "HTML CSS JavaScript Basics", "Responsive Design", "Modern JavaScript (ES6+)", "React.js Development",
        "Vue.js Development", "Node.js & Express Backend", "FastAPI Development", "Django Web Development",
        "Next.js Server-Side Rendering", "Web Performance Optimization", "RESTful API Design", "GraphQL APIs",
        "TypeScript for Web Apps", "WebSockets & Real-Time Apps", "Progressive Web Apps",
        "Authentication & Sessions", "Full-Stack Project Development",
    ],
    "Data Science & Analytics": [
        "Python for Data Analysis", "SQL for Data Analysts", "Data Visualization", "Statistics for Data Science",
        "A/B Testing & Experimentation", "Data Wrangling & Cleaning", "Business Intelligence (Power BI)",
        "Advanced Excel Analytics", "Data Storytelling", "Big Data with Spark", "ETL Pipeline Development",
        "Predictive Analytics", "Customer Analytics", "Marketing Analytics", "Financial Data Analysis",
        "Google Analytics & Web Analytics", "Data Ethics & Privacy",
    ],
    "Mobile Development": [
        "Android Development with Kotlin", "iOS Development with Swift", "Flutter Cross-Platform Apps",
        "React Native Development", "Mobile UI/UX Design", "State Management in Flutter",
        "App Store & Play Store Publishing", "Push Notifications & Firebase", "Mobile Performance Optimization",
        "Offline-First App Design", "In-App Purchases & Monetization", "Mobile Testing & Debugging",
        "SwiftUI Advanced Techniques", "Jetpack Compose UI", "Cross-Platform Architecture",
        "Mobile Security Best Practices", "Wearable App Development",
    ],
    "Cloud & DevOps": [
        "AWS Fundamentals", "Docker Containers", "Kubernetes Orchestration", "CI/CD with GitHub Actions",
        "Infrastructure as Code (Terraform)", "Azure Cloud Essentials", "Google Cloud Platform Basics",
        "Monitoring with Prometheus & Grafana", "Serverless Architecture", "Site Reliability Engineering",
        "Cloud Cost Optimization", "DevOps Culture & Practices", "Linux System Administration",
        "Ansible Configuration Management", "Microservices Deployment", "Cloud Security Fundamentals",
        "GitOps Workflows",
    ],
    "Cybersecurity": [
        "Cybersecurity Fundamentals", "Ethical Hacking & Pen Testing", "Network Security",
        "Web Application Security (OWASP)", "Cryptography Basics", "Cloud Security Best Practices",
        "Incident Response & Forensics", "Security in CI/CD (DevSecOps)", "Social Engineering Defense",
        "Malware Analysis", "Security Operations Center (SOC)", "Identity & Access Management",
        "Wireless Network Security", "Bug Bounty Hunting", "Security Compliance & Auditing",
        "Threat Intelligence", "Reverse Engineering Basics",
    ],
    "Programming Languages & CS Fundamentals": [
        "Python Programming Basics", "Data Structures & Algorithms", "Object-Oriented Programming",
        "Rust for Systems Programming", "Go Programming Essentials", "Competitive Programming",
        "C++ Fundamentals", "Functional Programming", "Introduction to CS Theory",
        "System Design Interview Prep", "Java Programming Fundamentals", "C Programming & Memory Management",
        "Recursion & Dynamic Programming", "Graph Algorithms", "Operating Systems Concepts",
        "Compiler Design Basics", "Interview Coding Patterns",
    ],
    "Database & Backend Systems": [
        "Relational Database Design", "Advanced SQL & Query Optimization", "MongoDB & NoSQL",
        "Scalable Backend Architecture", "Database Sharding & Replication", "Caching with Redis",
        "Message Queues (Kafka/RabbitMQ)", "API Authentication & OAuth2", "RESTful Backend Design",
        "Database Migrations", "PostgreSQL Deep Dive", "Database Performance Tuning",
        "Event-Driven Architecture", "GraphQL Backend Development", "Backend Testing Strategies",
        "Distributed Systems Basics", "Database Backup & Recovery",
    ],
    "UI/UX & Product Design": [
        "UI/UX Design Fundamentals", "Figma for Product Designers", "Design Systems",
        "User Research & Usability Testing", "Interaction Design", "Wireframing & Prototyping",
        "Accessibility in Design", "Design Thinking", "Mobile App Design Patterns",
        "Design-to-Code Handoff", "Visual Design Principles", "Typography & Color Theory",
        "Design Portfolio Building", "Product Management Basics for Designers", "User Journey Mapping",
        "Design Critique & Feedback", "Motion Design & Microinteractions",
    ],
}

# ---------------------------------------------------------------
# Category relatedness graph (symmetric). Weight 0.0–1.0 = how closely
# two fields relate — used to "spread" a user's interest score onto
# adjacent fields (e.g. a Data-Science person often also does ML) while
# leaving unrelated fields near zero (e.g. Data Science vs Cybersecurity).
#
# ASSUMPTION — these weights are a judgment call based on real-world
# field overlap, not something specified numerically anywhere. Tune
# freely if a pairing feels off.
# ---------------------------------------------------------------
_RELATEDNESS_PAIRS = [
    ("Machine Learning", "AI Engineering", 0.7),
    ("Machine Learning", "Data Science & Analytics", 0.6),
    ("Machine Learning", "Programming Languages & CS Fundamentals", 0.3),
    ("AI Engineering", "Data Science & Analytics", 0.4),
    ("AI Engineering", "Database & Backend Systems", 0.3),
    ("AI Engineering", "Web Development", 0.2),
    ("Data Science & Analytics", "Programming Languages & CS Fundamentals", 0.3),
    ("Data Science & Analytics", "Database & Backend Systems", 0.3),
    ("Data Science & Analytics", "Cybersecurity", 0.1),
    ("Web Development", "Database & Backend Systems", 0.5),
    ("Web Development", "UI/UX & Product Design", 0.4),
    ("Web Development", "Mobile Development", 0.3),
    ("Web Development", "Cloud & DevOps", 0.3),
    ("Web Development", "Programming Languages & CS Fundamentals", 0.3),
    ("Mobile Development", "UI/UX & Product Design", 0.3),
    ("Mobile Development", "Programming Languages & CS Fundamentals", 0.2),
    ("Cloud & DevOps", "Database & Backend Systems", 0.4),
    ("Cloud & DevOps", "Cybersecurity", 0.3),
    ("Cloud & DevOps", "Programming Languages & CS Fundamentals", 0.2),
    ("Cybersecurity", "Database & Backend Systems", 0.2),
    ("Cybersecurity", "Programming Languages & CS Fundamentals", 0.2),
    ("Programming Languages & CS Fundamentals", "Database & Backend Systems", 0.3),
    ("UI/UX & Product Design", "Database & Backend Systems", 0.1),
]

CATEGORY_RELATEDNESS: Dict[str, Dict[str, float]] = defaultdict(dict)
for _a, _b, _w in _RELATEDNESS_PAIRS:
    CATEGORY_RELATEDNESS[_a][_b] = _w
    CATEGORY_RELATEDNESS[_b][_a] = _w


def related_weight(category_a: str, category_b: str) -> float:
    """1.0 if same category, else the relatedness weight (0.0 if unrelated/unlisted)."""
    if category_a == category_b:
        return 1.0
    return CATEGORY_RELATEDNESS.get(category_a, {}).get(category_b, 0.0)


def get_related_categories(category: str) -> Dict[str, float]:
    """Returns {related_category: weight} for a given category (empty dict if none)."""
    return dict(CATEGORY_RELATEDNESS.get(category, {}))


# ---------------------------------------------------------------
# Onboarding interest labels -> real Product category weights.
# Solves the naming mismatch (e.g. onboarding "AI/ML" has no Product
# with category=="AI/ML" — it really means "Machine Learning" +
# "AI Engineering"). Also lets one onboarding choice contribute to
# multiple related real categories, weighted.
#
# ASSUMPTION — weights are a judgment call, tune if needed.
# ---------------------------------------------------------------
ONBOARDING_TO_PRODUCT_CATEGORY_WEIGHTS: Dict[str, Dict[str, float]] = {
    "Data Science": {
        "Data Science & Analytics": 1.0,
        "Machine Learning": 0.5,
        "Programming Languages & CS Fundamentals": 0.2,
    },
    "AI/ML": {
        "Machine Learning": 1.0,
        "AI Engineering": 0.9,
        "Data Science & Analytics": 0.4,
    },
    "Cybersecurity": {
        "Cybersecurity": 1.0,
    },
    "Web Development": {
        "Web Development": 1.0,
        "UI/UX & Product Design": 0.3,
        "Database & Backend Systems": 0.3,
    },
    "Frontend": {
        "Web Development": 0.8,
        "UI/UX & Product Design": 0.6,
    },
    "Backend": {
        "Database & Backend Systems": 1.0,
        "Web Development": 0.5,
        "Cloud & DevOps": 0.3,
    },
}


# ---------------------------------------------------------------
# Search-query -> category inference.
#
# Uses POSITION-WEIGHTED, WORD-BOUNDARY keyword matching (not naive
# substring/equal-weight matching), because a real bug was observed
# during testing: a debounced-typing query like "aws for data
# science" (progressively refined: "aws" -> "aws for" -> "aws for
# data science") scored HIGHER for "Data Science & Analytics" than
# "Cloud & DevOps", even though "aws" was clearly the user's actual
# subject and "data science" were qualifier words typed afterward.
# Since users type their core subject FIRST and add refining words
# after (this is how the debounced search box in products.html
# works), earlier words are weighted higher (1/(position+1)) so the
# original intent dominates over incidental qualifier-word overlap.
#
# Word-boundary matching (regex tokenization) also fixes a related
# bug where a query word like "data" could accidentally substring-
# match inside an unrelated word like "database".
# ---------------------------------------------------------------
_QUERY_STOPWORDS = {
    "for", "with", "and", "the", "of", "in", "on", "a", "an", "to",
    "is", "are", "course", "courses", "full", "complete",
}


def infer_category_from_query(query: str) -> Optional[str]:
    """
    Best-effort match of a free-text search query to one of the 10 real
    categories, using CATEGORY_TOPICS as the keyword source. Good
    enough for a hackathon demo, not meant to be a full NLP classifier.
    Returns None if no category scores above zero.
    """
    if not query:
        return None

    raw_words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    words = [w for w in raw_words if w not in _QUERY_STOPWORDS]
    if not words:
        return None

    best_category, best_score = None, 0.0
    for category, topics in CATEGORY_TOPICS.items():
        haystack_text = (category + " " + " ".join(topics)).lower()
        haystack_words = set(re.findall(r"[a-z0-9]+", haystack_text))

        score = 0.0
        for position, word in enumerate(words):
            if word in haystack_words:
                score += 1.0 / (position + 1)

        if score > best_score:
            best_category, best_score = category, score

    return best_category