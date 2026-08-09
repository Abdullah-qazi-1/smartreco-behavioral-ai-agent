"""
Core database models for SmartReco.
Supports dual-mode users (Student/Instructor), expanded course catalog,
behavioral tracking, recommendations, wishlists, and vector DB sync logs.
"""
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, Numeric, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user", nullable=False)  # Backward compatibility: "user" | "admin"
    active_mode = Column(String, default="student", nullable=False)  # "student" | "instructor"
    interests = Column(String, nullable=True)  # comma-separated e.g. "AI/ML,Backend"
    interests_updated_at = Column(DateTime(timezone=True), nullable=True)
    experience_level = Column(String, nullable=True)  # "Beginner" | "Intermediate" | "Advanced"
    avatar_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    profile = relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    courses = relationship("Product", back_populates="instructor", foreign_keys="Product.instructor_id")
    enrollments = relationship("Enrollment", back_populates="user", cascade="all, delete-orphan")
    wishlists = relationship("Wishlist", back_populates="user", cascade="all, delete-orphan")
    events = relationship("Event", back_populates="user", cascade="all, delete-orphan")
    recommendations = relationship("Recommendation", back_populates="user", cascade="all, delete-orphan")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    onboarding_completed = Column(Boolean, default=False, nullable=False)
    primary_goal = Column(String, nullable=True)
    interests_last_updated_at = Column(DateTime(timezone=True), nullable=True)
    agent_tracking_enabled = Column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="profile")


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    parent_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)

    parent = relationship("Category", remote_side=[id])


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)


class Product(Base):
    """
    Core Course/Product table. Named 'products' in database for backward compatibility.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    slug = Column(String, nullable=True, index=True)
    description = Column(Text, nullable=False)
    category = Column(String, nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    skills = Column(String, nullable=True)
    price = Column(Float, default=0.0, index=True)
    level = Column(String, nullable=True, index=True)  # "Beginner" | "Intermediate" | "Advanced"

    instructor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    instructor_name = Column(String, nullable=True, index=True)
    rating = Column(Float, nullable=True, default=0.0, index=True)
    num_ratings = Column(Integer, nullable=True, default=0)
    enrolled_students = Column(Integer, nullable=True, default=0)
    duration_hours = Column(Float, nullable=True, default=0.0)
    status = Column(String, default="active", nullable=False, index=True)  # "active" | "draft" | "archived"

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    instructor = relationship("User", back_populates="courses", foreign_keys=[instructor_id])
    reviews = relationship("Review", back_populates="product", cascade="all, delete-orphan")
    learning_outcomes = relationship("CourseLearningOutcome", back_populates="product", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "course_id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "category_id": self.category_id,
            "level": self.level,
            "price": self.price,
            "skills": self.skills,
            "instructor_id": self.instructor_id,
            "instructor_name": self.instructor_name,
            "rating": self.rating or 0.0,
            "num_ratings": self.num_ratings or 0,
            "enrolled_students": self.enrolled_students or 0,
            "duration_hours": self.duration_hours or 0.0,
            "status": self.status or "active",
        }

    @property
    def course_id(self):
        return self.id


# Alias Course -> Product for production naming
Course = Product


class CourseSkill(Base):
    __tablename__ = "course_skills"

    course_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    skill_id = Column(Integer, ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True)

    @property
    def product_id(self):
        return self.course_id

    @product_id.setter
    def product_id(self, val):
        self.course_id = val


class CourseLearningOutcome(Base):
    __tablename__ = "course_learning_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    outcome_text = Column(Text, nullable=False)
    display_order = Column(Integer, default=0)

    product = relationship("Product", back_populates="learning_outcomes")

    @property
    def product_id(self):
        return self.course_id

    @product_id.setter
    def product_id(self, val):
        self.course_id = val

    @property
    def course(self):
        return self.product

    @course.setter
    def course(self, val):
        self.product = val


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewer_name = Column(String, nullable=True)
    rating = Column(Float, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", back_populates="reviews")

    @property
    def course_id(self):
        return self.product_id

    @course_id.setter
    def course_id(self, val):
        self.product_id = val

    @property
    def course(self):
        return self.product

    @course.setter
    def course(self, val):
        self.product = val


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_user_created", "user_id", "created_at"),
        Index("ix_events_user_eligible_created", "user_id", "agent_eligible", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)  # view | search | click | time_spent | dismiss
    product_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    event_metadata = Column(Text, nullable=True)
    agent_eligible = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user = relationship("User", back_populates="events")

    @property
    def course_id(self):
        return self.product_id

    @course_id.setter
    def course_id(self, val):
        self.product_id = val


class SearchHistory(Base):
    __tablename__ = "search_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    raw_query = Column(String, nullable=False)
    inferred_category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    narrative = Column(Text, nullable=False)
    product_ids = Column(Text, nullable=False)
    trigger_reason = Column(String, nullable=True)
    is_latest = Column(Boolean, default=True, index=True)
    converted = Column(Boolean, default=False, nullable=True, index=True)
    converted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


    user = relationship("User", back_populates="recommendations")
    explanations = relationship("RecommendationExplanation", back_populates="recommendation", cascade="all, delete-orphan")

    @property
    def course_ids(self):
        return self.product_ids

    @course_ids.setter
    def course_ids(self, val):
        self.product_ids = val


class RecommendationExplanation(Base):
    __tablename__ = "recommendation_explanations"

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(Integer, ForeignKey("recommendations.id", ondelete="CASCADE"), nullable=False, index=True)
    factor_title = Column(String, nullable=False)
    factor_description = Column(Text, nullable=False)
    source_event_type = Column(String, nullable=True)

    recommendation = relationship("Recommendation", back_populates="explanations")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    progress_pct = Column(Float, default=0.0)
    current_module = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_accessed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="enrollments")
    product = relationship("Product")

    @property
    def course_id(self):
        return self.product_id

    @course_id.setter
    def course_id(self, val):
        self.product_id = val

    @property
    def course(self):
        return self.product

    @course.setter
    def course(self, val):
        self.product = val

    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='_user_product_enrollment_uc'),
    )


class Wishlist(Base):
    __tablename__ = "wishlists"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="wishlists")
    product = relationship("Product")

    @property
    def course_id(self):
        return self.product_id

    @course_id.setter
    def course_id(self, val):
        self.product_id = val

    @property
    def course(self):
        return self.product

    @course.setter
    def course(self, val):
        self.product = val

    __table_args__ = (
        UniqueConstraint('user_id', 'product_id', name='_user_product_wishlist_uc'),
    )


class LearningPath(Base):
    __tablename__ = "learning_paths"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    target_role = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UserLearningPath(Base):
    __tablename__ = "user_learning_paths"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    path_id = Column(Integer, ForeignKey("learning_paths.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="in_progress")
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChromaSyncLog(Base):
    __tablename__ = "chroma_sync_log"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String, nullable=False)  # "upsert" | "delete"
    status = Column(String, nullable=False)  # "synced" | "pending" | "failed"
    synced_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def course_id(self):
        return self.product_id

    @course_id.setter
    def course_id(self, val):
        self.product_id = val

