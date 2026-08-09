"""
Seed script — generates ~1000 realistic courses across 10 categories,
each with instructor, rating, num_ratings, duration.
Run once: python seed_data.py
Writes to SQLite AND Chroma together via product_service (dual-write).
"""
import random
from database.db import SessionLocal, Base, engine
from database.models import Product
from services import product_service
from services.category_taxonomy import CATEGORY_TOPICS

random.seed(42)  # reproducible dataset across runs

Base.metadata.create_all(bind=engine)

LEVELS = ["Beginner", "Intermediate", "Advanced"]

CATEGORY_TOPICS = {
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

CATEGORY_SKILL_POOL = {
    "Machine Learning": ["Python", "scikit-learn", "NumPy", "Pandas", "Regression", "Classification",
                          "Random Forest", "XGBoost", "Feature Engineering", "Cross-Validation",
                          "Model Evaluation", "Clustering", "PCA", "Neural Networks", "Statistics"],
    "AI Engineering": ["LangChain", "LangGraph", "RAG", "Vector Databases", "Embeddings", "Prompt Engineering",
                        "OpenAI API", "LLM", "Fine-Tuning", "LoRA", "AI Agents", "Ollama",
                        "Semantic Search", "Python", "PyTorch"],
    "Web Development": ["HTML", "CSS", "JavaScript", "React", "Node.js", "Express", "FastAPI", "REST API",
                         "GraphQL", "TypeScript", "Next.js", "Responsive Design", "Web Performance",
                         "Authentication", "Git"],
    "Data Science & Analytics": ["Python", "Pandas", "SQL", "Statistics", "Data Visualization", "Matplotlib",
                                  "Seaborn", "Power BI", "Excel", "A/B Testing", "Data Cleaning", "Spark",
                                  "ETL", "Data Storytelling", "NumPy"],
    "Mobile Development": ["Kotlin", "Swift", "Flutter", "Dart", "React Native", "Jetpack Compose", "SwiftUI",
                            "Firebase", "Mobile UI/UX", "App Store Deployment", "State Management", "Riverpod",
                            "Performance Optimization", "Offline Storage", "Push Notifications"],
    "Cloud & DevOps": ["AWS", "Docker", "Kubernetes", "Terraform", "CI/CD", "GitHub Actions", "Azure", "GCP",
                        "Prometheus", "Grafana", "Serverless", "Linux", "Ansible", "Microservices",
                        "Cloud Security"],
    "Cybersecurity": ["Network Security", "Penetration Testing", "Kali Linux", "OWASP", "Cryptography",
                       "Cloud Security", "Incident Response", "Digital Forensics", "DevSecOps",
                       "Social Engineering", "Malware Analysis", "SOC", "IAM", "Threat Intelligence",
                       "Reverse Engineering"],
    "Programming Languages & CS Fundamentals": ["Python", "Data Structures", "Algorithms", "Big-O", "OOP",
                                                  "Rust", "Go", "C++", "Functional Programming", "System Design",
                                                  "Java", "C", "Recursion", "Graph Algorithms", "Operating Systems"],
    "Database & Backend Systems": ["SQL", "PostgreSQL", "MongoDB", "Redis", "Kafka", "RabbitMQ",
                                    "Database Design", "Indexing", "Query Optimization", "Sharding",
                                    "Replication", "REST API", "OAuth2", "JWT", "Distributed Systems"],
    "UI/UX & Product Design": ["Figma", "Wireframing", "Prototyping", "Design Systems", "User Research",
                                "Usability Testing", "Accessibility", "WCAG", "Design Thinking", "Typography",
                                "Color Theory", "Interaction Design", "Motion Design", "Design Handoff",
                                "Visual Design"],
}

INSTRUCTOR_POOL = {
    "Machine Learning": [("Andrew Ng", "Coursera"), ("Krish Naik", "Udemy"), ("Josh Starmer (StatQuest)", "YouTube"),
                          ("Jose Portilla", "Udemy"), ("Codebasics - Dhaval Patel", "YouTube")],
    "AI Engineering": [("Andrew Ng", "Coursera"), ("DeepLearning.AI Team", "Coursera"), ("Krish Naik", "Udemy"),
                        ("Harrison Chase", "LangChain Academy"), ("Sam Witteveen", "YouTube")],
    "Web Development": [("Colt Steele", "Udemy"), ("Mosh Hamedani", "Udemy"), ("Brad Traversy", "Udemy"),
                         ("Maximilian Schwarzmüller", "Udemy"), ("freeCodeCamp", "freeCodeCamp.org")],
    "Data Science & Analytics": [("Krish Naik", "Udemy"), ("Codebasics - Dhaval Patel", "YouTube"),
                                  ("Jose Portilla", "Udemy"), ("Andrew Ng", "Coursera"), ("Kirill Eremenko", "Udemy")],
    "Mobile Development": [("Angela Yu", "Udemy"), ("Mitch Koko", "Udemy"), ("Vandad Nahavandipoor", "Udemy"),
                            ("Academind", "Udemy"), ("Reso Coder", "YouTube")],
    "Cloud & DevOps": [("Stephane Maarek", "Udemy"), ("KodeKloud Team", "Udemy"), ("Andrew Brown", "freeCodeCamp"),
                        ("Adrian Cantrill", "Independent"), ("TechWorld with Nana", "YouTube")],
    "Cybersecurity": [("Heath Adams (TCM Security)", "Udemy"), ("Jason Dion", "Udemy"), ("NetworkChuck", "YouTube"),
                       ("The Cyber Mentor", "Udemy"), ("Zaid Sabih", "Udemy")],
    "Programming Languages & CS Fundamentals": [("Mosh Hamedani", "Udemy"), ("CS50 - David Malan", "edX"),
                                                  ("Corey Schafer", "YouTube"), ("Abdul Bari", "YouTube"),
                                                  ("Tim Buchalka", "Udemy")],
    "Database & Backend Systems": [("Colt Steele", "Udemy"), ("Mosh Hamedani", "Udemy"), ("Academind", "Udemy"),
                                    ("Stephane Maarek", "Udemy"), ("Hussein Nasser", "YouTube")],
    "UI/UX & Product Design": [("Daniel Walter Scott", "Udemy"), ("Gary Simon (DesignCourse)", "YouTube"),
                                ("CareerFoundry Team", "CareerFoundry"), ("AJ&Smart", "YouTube"),
                                ("Julie Zhuo", "LinkedIn Learning")],
}

TITLE_STYLES = [
    "{topic}: Complete Guide", "{topic} — Zero to {level} Hero", "Mastering {topic}",
    "{topic} for {level}s", "Practical {topic}: Hands-On Projects", "{topic} Bootcamp",
    "The Complete {topic} Course", "{topic} Crash Course", "{topic} Explained Step-by-Step",
    "{topic}: From Fundamentals to {level} Mastery",
]

DESCRIPTION_TEMPLATES = [
    "Master {topic} through hands-on projects covering {skills}, built for {level_l} learners aiming for real-world {category} skills.",
    "A {level_l}-friendly course on {topic} that walks through {skills} step-by-step with practical exercises and real datasets.",
    "Learn {topic} from the ground up — {skills} explained clearly with examples, quizzes, and a final capstone project.",
    "Go deep into {topic}, covering {skills}, with case studies drawn from real industry problems in {category}.",
    "A {level_l} course focused entirely on {topic}. You'll practice {skills} through guided exercises and build a portfolio-ready project.",
    "Everything you need to know about {topic} — {skills} — taught with a project-first approach for {level_l} learners.",
]


def price_for_level(level):
    ranges = {"Beginner": (15, 35), "Intermediate": (28, 55), "Advanced": (42, 79)}
    lo, hi = ranges[level]
    return round(random.uniform(lo, hi), 0)


def duration_for_level(level):
    ranges = {"Beginner": (3, 9), "Intermediate": (8, 20), "Advanced": (15, 38)}
    lo, hi = ranges[level]
    hours = round(random.uniform(lo, hi), 1)
    return hours


def rating_and_count(enrolled_students: int):
    rating = round(min(5.0, max(3.0, random.gauss(4.3, 0.35))), 1)
    ratio = random.triangular(0.04, 0.28, 0.12)
    num_ratings = int(max(8, min(enrolled_students, max(10, round(enrolled_students * ratio)))))
    return rating, num_ratings


def build_courses():
    courses = []
    for category, topics in CATEGORY_TOPICS.items():
        skill_pool = CATEGORY_SKILL_POOL[category]
        instructors = INSTRUCTOR_POOL[category]
        for topic in topics:
            for level in LEVELS:
                for _ in range(2):  # 2 style variants per topic+level
                    # instructors list contains tuples (name, platform).
                    # Use only the instructor name string for DB column `instructor_name`.
                    ins = random.choice(instructors)
                    instructor_name = ins[0] if isinstance(ins, (list, tuple)) else ins
                    title = random.choice(TITLE_STYLES).format(topic=topic, level=level)
                    skills_sample = random.sample(skill_pool, k=min(4, len(skill_pool)))
                    skills = ", ".join(skills_sample)
                    description = random.choice(DESCRIPTION_TEMPLATES).format(
                        topic=topic, skills=skills, level_l=level.lower(), category=category
                    )
                    price = price_for_level(level)
                    hours = duration_for_level(level)
                    enrolled_students = int(min(20000, max(45, random.lognormvariate(5.8, 1.0))))
                    rating, num_ratings = rating_and_count(enrolled_students)
                    courses.append(dict(
                        title=title, description=description, category=category,
                        price=price, level=level, skills=skills,
                        instructor_name=instructor_name,
                        enrolled_students=enrolled_students,
                        rating=rating, num_ratings=num_ratings,
                        duration_hours=hours,
                    ))

    # dedupe identical titles (rare, when random style/topic collide)
    seen = {}
    for c in courses:
        t = c["title"]
        seen[t] = seen.get(t, 0) + 1
        if seen[t] > 1:
            c["title"] = f"{t} (v{seen[t]})"
    return courses


def seed():
    db = SessionLocal()
    try:
        existing = db.query(Product).count()
        if existing > 0:
            print(f"[INFO] {existing} products already exist. Clearing table before reseeding...")
            db.query(Product).delete()
            db.commit()

        courses = build_courses()
        print(f"Generated {len(courses)} courses. Seeding into SQLite + Chroma (this takes a few minutes)...")

        for i, c in enumerate(courses, 1):
            product_service.create_product(db, **c)
            if i % 50 == 0:
                print(f"  ...{i}/{len(courses)} seeded")

        total = db.query(Product).count()
        print(f"✅ Seeded {total} products (with instructor/platform/rating/duration) into SQLite + Chroma.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()