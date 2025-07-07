from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import openai
from google import genai
import re
import os
from dotenv import load_dotenv
from typing import List, Dict, Any
import pandas as pd
import base64
import uuid
from io import StringIO
import logging
import sqlparse
from fastapi.responses import FileResponse
import json


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv(override=True)

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_TYPE = os.getenv("DATABASE_TYPE", "postgresql").lower()
MYSQLDB_URL = os.getenv("MYSQLDB_URL")
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "openai").lower()

# Validate configuration
if not all([OPENAI_API_KEY, GEMINI_API_KEY, DATABASE_URL]):
    raise ValueError("Missing required environment variables")

openai.api_key = OPENAI_API_KEY
client = genai.Client(api_key=GEMINI_API_KEY)

# Select engine and connection string based on DATABASE_TYPE
def get_engine_and_schema_query():
    if DATABASE_TYPE == "postgresql":
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        schema_query = """
            SELECT table_name, column_name, data_type 
            FROM information_schema.columns 
            WHERE table_schema = 'public' 
            ORDER BY table_name;
        """
        prompt_db = "PostgreSQL"
        prompt_syntax = "Use proper PostgreSQL syntax"
    elif DATABASE_TYPE == "mysql":
        if not MYSQLDB_URL:
            raise ValueError("Missing MYSQLDB_URL for MySQL database type")
        engine = create_engine(MYSQLDB_URL, pool_pre_ping=True)
        # Extract db name from URL for schema query
        import urllib.parse
        db_name = urllib.parse.urlparse(MYSQLDB_URL).path.lstrip('/')
        schema_query = f"""
            SELECT table_name, column_name, data_type 
            FROM information_schema.columns 
            WHERE table_schema = '{db_name}' 
            ORDER BY table_name;
        """
        prompt_db = "MySQL"
        prompt_syntax = "Use proper MySQL syntax. Use backticks (`) for table and column names, not double quotes. Do not use double quotes for identifiers. use the schema and column names as they are in the database"
    else:
        raise ValueError(f"Unsupported DATABASE_TYPE: {DATABASE_TYPE}")
    return engine, schema_query, prompt_db, prompt_syntax

engine, schema_query, PROMPT_DB, PROMPT_SYNTAX = get_engine_and_schema_query()

app = FastAPI(title="SQL Query Generator")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust to match your frontend's URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

result_cache: Dict[str, list] = {}

def get_schema() -> str:
    try:
        with engine.connect() as connection:
            result = connection.execute(text(schema_query))
            schema_info = {}
            for row in result:
                table, column, data_type = row
                if table not in schema_info:
                    schema_info[table] = []
                schema_info[table].append(f"{column} ({data_type})")
            formatted_schema = "\n".join(
                f"Table: {table}\nColumns: {', '.join(columns)}\n"
                for table, columns in schema_info.items()
            )
            return formatted_schema.strip()
    except SQLAlchemyError as e:
        logger.error(f"Database error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error fetching schema")
    
db_schema = get_schema()


class QueryRequest(BaseModel):
    db_schema = get_schema()
    user_query: str = Field(..., min_length=1, max_length=1000)
    db_schema: str = db_schema

class QueryResponse(BaseModel):
    sql_query: str
    results: List[Dict[str, Any]]
    csv_base64: str
    csv_filename: str
    summary: str

def openai_api_call(prompt: str) -> str:
    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2,
            max_tokens=1000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI API error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error processing request with OpenAI")

def gemini_api_call(prompt: str) -> str:
    try:
        
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini API error: {str(e)}")
        raise HTTPException(status_code=500, detail="Error processing request with Gemini")


    
def is_safe_select(sql: str) -> bool:
    parsed = sqlparse.parse(sql)
    for stmt in parsed:
        if stmt.get_type() != "SELECT":
            return False
    return True

def generate_sql(user_query: str, schema: str) -> str:
    forbidden_keywords = ['add', 'insert', 'update', 'delete', 'drop', 'alter', 'truncate']
    if any(kw in user_query.lower() for kw in forbidden_keywords):
        logger.warning(f"Blocked potentially harmful user query: {user_query}")
        raise HTTPException(status_code=400, detail="Only read-only queries are allowed. Mutating queries are not supported.")
    print("The schema is: ", schema)
    prompt = f"""
    Convert the following user intent into a read-only {PROMPT_DB} query.
    Schema Information:
    {schema}
    
    Rules:
    - ONLY generate SELECT statements. Do NOT generate INSERT, UPDATE, DELETE, or DDL queries. If user's query is not a read-only query, return an error message.
    - Use double quotes for table and column names
    - Ensure SQL is secure against injection
    - Return only the SQL query
    - {PROMPT_SYNTAX}
    
    User Query: {user_query}
    """
    
    try:
        if MODEL_PROVIDER == "openai":
            response = openai_api_call(prompt)
        elif MODEL_PROVIDER == "gemini":
            response = gemini_api_call(prompt)
        else:
            raise ValueError(f"Unsupported model provider: {MODEL_PROVIDER}")
        
        print("The generated SQL query from the LLM is: ", response)
        # Validate SQL query
        if not response.strip():
            raise ValueError("Generated SQL query is empty")
                    
        # Basic SQL injection check
        dangerous_patterns = [r';\s*--', r';\s*/\*', r'EXEC\s+', r'DROP\s+TABLE']
        for pattern in dangerous_patterns:
            if re.search(pattern, response, re.IGNORECASE):
                raise ValueError("Potentially dangerous SQL detected")
                
        return response.strip()
    except Exception as e:
        logger.error(f"SQL generation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error generating SQL: {str(e)}")
    



def execute_sql(query: str) -> List[Dict[str, Any]]:
    try:
        # Clean query
        query = re.sub(r"^```sql\s*|\s*```$", "", query.strip(), flags=re.MULTILINE)
        print("The cleaned SQL query is: ", query)

        if not is_safe_select(query):
            raise ValueError("Generated SQL query is not a read-only SELECT statement")
        
        with engine.connect() as connection:
            with connection.begin():  # Ensure transaction
                result = connection.execute(text(query))
                columns = result.keys()
                return [dict(zip(columns, row)) for row in result.fetchall()]
    except SQLAlchemyError as e:
        logger.error(f"SQL execution error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"SQL execution failed: {str(e)}")

# Endpoints
@app.get("/fetch-schema")
async def fetch_schema():
    schema = get_schema()
    return {"schema": schema}

@app.post("/generate-query", response_model=QueryResponse)
async def generate_and_execute(request: QueryRequest):
    # Generate SQL
    db_schema = get_schema()
    sql_query = generate_sql(request.user_query, db_schema)
    
    # Execute SQL
    results = execute_sql(sql_query)
    print("The results are: ", results)

    # Flatten JSON/dict fields for frontend presentation
    def flatten_record(record: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
        items: Dict[str, Any] = {}
        for k, v in record.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(flatten_record(v, new_key, sep=sep))
            else:
                items[new_key] = v
        return items
    flat_results = [flatten_record(r) for r in results]

    # Generate a natural-language summary of the results using the LLM
    try:
        results_json = json.dumps(flat_results, default=str, indent=2)
        summary_prompt = (
            "You are an expert SQL assistant. A user asks a question about a database, and you've already shown the relevant table or data. Now, summarize the answer in 1–2 clear, non-technical lines that directly answer the user's query based on the shown data. Be concise, helpful, and avoid repeating the full table unless necessary.\n\n"
            f"the user's question is {request.user_query} and the results are {results_json}, so provide answer to the user's question based on the results. "
        )
        if MODEL_PROVIDER == "openai":
            summary = openai_api_call(summary_prompt)
        else:
            summary = gemini_api_call(summary_prompt)
    except Exception as e:
        logger.error(f"LLM summary generation error: {str(e)}")
        summary = f"Returned {len(flat_results)} rows."

    #write results to csv file
    if flat_results:
        df = pd.DataFrame(flat_results)
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False)
        with open(f"result_{uuid.uuid4().hex[:8]}.csv", "w") as f:
            f.write(csv_buffer.getvalue())
        csv_base64 = base64.b64encode(csv_buffer.getvalue().encode()).decode()
        csv_filename = f"result_{uuid.uuid4().hex[:8]}.csv"
    else:
        csv_base64 = ""
        csv_filename = ""
    
    return {
        "sql_query": sql_query,
        "results": flat_results,
        "csv_base64": csv_base64,
        "csv_filename": csv_filename,
        "summary": summary
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.get("/")
async def serve_index():
    return FileResponse("index.html")