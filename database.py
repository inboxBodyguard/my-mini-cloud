# database.py
import os
import json
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# Database configuration with fallback
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mini-cloud.db")

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
    
    print(f"✅ Database engine created for: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")
except Exception as e:
    print(f"❌ Database connection failed: {e}")
    print("🔄 Falling back to SQLite...")
    DATABASE_URL = "sqlite:///./mini-cloud.db"
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class App(Base):
    __tablename__ = "apps"
    
    id = Column(String, primary_key=True, index=True)
    name = Column(String, index=True)
    status = Column(String, default="building")
    url = Column(String)
    port = Column(Integer)
    git_url = Column(String, nullable=True)
    environment_variables = Column(Text)
    container_id = Column(String, nullable=True)
    user_id = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class APIKey(Base):
    __tablename__ = "api_keys"
    
    key = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

# Create tables with error handling
try:
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created successfully")
except Exception as e:
    print(f"❌ Failed to create database tables: {e}")
    print("🔄 Continuing with existing tables...")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()