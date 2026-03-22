import logging

# Configure log settings
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize database connection
conn = None
try:
    conn = init_db_connection()  # Assuming init_db_connection is your function for initializing DB
    # your existing Main logic, keeping the functionality intact

except Exception as e:
    logging.error('Error initializing the database connection: %s', e)

finally:
    if conn:
        conn.close()  # Ensure the connection is closed properly

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-restrictive-domain.com"],  # Set specific allowed domains
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# OAuth states and user sessions storage in database instead of in-memory
# Assuming that the necessary tables are created and ORM or similar is being used
# Replace in-memory logic with database storage methods

oauth_states_db = {}  # This will store your states in the database instead of in-memory
user_sessions_db = {}  # This will also store user sessions in DB

# Your existing logic from line 191 continues here...