# app.py
from flask import Flask, render_template, jsonify, request, send_from_directory, redirect, url_for
import os
import sqlite3
import hashlib # For password hashing
from datetime import datetime
from flask_cors import CORS # Import CORS for cross-origin requests
import jdatetime # For Persian dates
import io # For handling file-like objects in memory
from docx import Document # For DOCX generation

# Initialize the Flask application
app = Flask(__name__)
CORS(app) # Enable CORS for all routes, allowing frontend to access backend

# --- Configuration ---
# Base directory for all tenant databases
TENANT_DB_BASE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data', 'tenants')
# This path will be used to store generated DOCX files on the server locally.
GENERATED_LETTERS_BASE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'generated_letters')

SECRET_KEY = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_development') 
app.config['SECRET_KEY'] = SECRET_KEY

# Ensure necessary directories exist at startup
os.makedirs(TENANT_DB_BASE_DIR, exist_ok=True)
os.makedirs(GENERATED_LETTERS_BASE_DIR, exist_ok=True) 

# --- Database Functions ---

def get_db_path(company_name):
    """Constructs the database file path for a given company."""
    # Sanitize company_name to prevent path traversal issues
    clean_company_name = "".join(c for c in company_name if c.isalnum() or c in (' ', '-', '_')).strip()
    if not clean_company_name:
        raise ValueError("Company name cannot be empty or contain only special characters.")
    return os.path.join(TENANT_DB_BASE_DIR, f"{clean_company_name}_crm.db")

def get_db_connection(company_name):
    """Establishes a connection to the SQLite database for a specific company."""
    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database for company '{company_name}' not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # Allows accessing columns by name
    return conn

def create_tables_for_tenant(db_path):
    """Creates necessary tables in a specific database if they don't exist,
    and adds missing columns to existing tables."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create Organizations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            industry TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            description TEXT
        )
    """)

    # Create Contacts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            title TEXT,
            phone TEXT,
            email TEXT,
            notes TEXT,
            FOREIGN KEY (organization_id) REFERENCES Organizations(id) ON DELETE SET NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_contacts_organization_id ON Contacts (organization_id);")

    # Create Letters table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            letter_code_prefix TEXT NOT NULL,
            letter_code_number INTEGER NOT NULL,
            letter_code_persian TEXT NOT NULL UNIQUE, 
            type TEXT NOT NULL,
            date_shamsi_persian TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            organization_id INTEGER,
            contact_id INTEGER,
            file_path TEXT NOT NULL, -- This will now store the local file path
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER, 
            FOREIGN KEY (organization_id) REFERENCES Organizations(id) ON DELETE SET NULL,
            FOREIGN KEY (contact_id) REFERENCES Contacts(id) ON DELETE SET NULL,
            FOREIGN KEY (user_id) REFERENCES Users(id) ON DELETE SET NULL 
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_letters_user_id ON Letters (user_id);")

    # Create Users table (MODIFIED: changed username to email, added unique constraint)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE, -- Changed from username to email
            password_hash TEXT NOT NULL, 
            role TEXT NOT NULL DEFAULT 'user' 
        )
    """)
    conn.commit()
    conn.close()

def _hash_password(password):
    """Hashes a password using SHA256."""
    return hashlib.sha256(password.encode()).hexdigest()

def add_initial_admin_for_tenant(company_name, admin_email, admin_password):
    """Adds an initial admin user to a newly created tenant database."""
    db_path = get_db_path(company_name)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        hashed_password = _hash_password(admin_password)
        # MODIFIED: Insert email instead of username
        cursor.execute("INSERT INTO Users (email, password_hash, role) VALUES (?, ?, ?)",
                       (admin_email, hashed_password, "admin"))
        conn.commit()
        print(f"Initial admin user '{admin_email}' created for company '{company_name}'.")
        return True
    except sqlite3.IntegrityError:
        print(f"Admin user '{admin_email}' already exists for company '{company_name}'.")
        return False
    except Exception as e:
        print(f"Error creating initial admin for company '{company_name}': {e}")
        return False
    finally:
        conn.close()

# --- Helper Functions for Letter Generation ---

def convert_numbers_to_persian(text):
    """Converts English digits in a string to Persian digits."""
    persian_numbers = "۰۱۲۳۴۵۶۷۸۹"
    english_numbers = "0123456789"
    mapping = str.maketrans(english_numbers, persian_numbers)
    return text.translate(mapping)

def replace_text_in_docx(doc_stream, replacements):
    """
    Finds and replaces text in a .docx document including headers, footers, and tables.
    Handles placeholders that might be split across multiple runs within a paragraph/cell.
    Takes a file-like object (BytesIO) as input and returns a modified BytesIO.
    """
    document = Document(doc_stream)

    def replace_in_paragraphs(paragraphs, replacements_dict):
        for paragraph in paragraphs:
            for old_text, new_text in replacements_dict.items():
                if old_text in paragraph.text:
                    # Simple replacement for now. More complex logic needed for split runs.
                    paragraph.text = paragraph.text.replace(old_text, new_text)

    # Replace in main document body
    replace_in_paragraphs(document.paragraphs, replacements)

    # Replace in tables
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                replace_in_paragraphs(cell.paragraphs, replacements)

    # Replace in headers and footers (if any)
    for section in document.sections:
        if section.header:
            replace_in_paragraphs(section.header.paragraphs, replacements)
        if section.footer:
            replace_in_paragraphs(section.footer.paragraphs, replacements)

    # Save the modified document to a BytesIO object
    modified_doc_stream = io.BytesIO()
    document.save(modified_doc_stream)
    modified_doc_stream.seek(0) # Rewind to the beginning
    return modified_doc_stream

def generate_letter_number_for_company(company_name):
    """Generates a new letter code based on current date and database sequence for a specific company."""
    year_shamsi = jdatetime.date.today().year
    
    conn = get_db_connection(company_name) # Connect to specific company's DB
    cursor = conn.cursor()
    
    next_sequence_number = 1

    # Find the maximum existing sequence number for the current Shamsi year and company prefix
    company_abbr = "NGRR" # Placeholder, will be from company-specific settings later
    current_year_prefix_pattern = f"{year_shamsi}/{company_abbr}-%" 
    
    cursor.execute(f"""
        SELECT MAX(letter_code_number) 
        FROM Letters 
        WHERE letter_code_persian LIKE ?
    """, (f"{year_shamsi}/{company_abbr}-%",))
    
    max_number = cursor.fetchone()[0]
    if max_number is not None:
        next_sequence_number = max_number + 1
    
    conn.close()
    
    return next_sequence_number, year_shamsi

# --- Routes for Frontend Serving ---
@app.route('/')
def login_page():
    """
    Serves the login HTML page.
    """
    return render_template('login.html')

@app.route('/main_app')
def main_app():
    """
    Serves the main application HTML page.
    This route will be accessed after successful login.
    """
    return render_template('index.html')


# --- API Endpoints ---

@app.route('/api/status', methods=['GET'])
def get_status():
    """
    A simple API endpoint to check the backend status.
    """
    return jsonify({"status": "Backend is running!", "message": "Ready for action."}), 200

@app.route('/api/login', methods=['POST'])
def login():
    """
    Handles user login for a specific company.
    Requires 'email', 'password', and 'company_name'.
    """
    data = request.get_json()
    email = data.get('email') # Changed from username to email
    password = data.get('password')
    company_name = data.get('company_name')

    if not company_name:
        return jsonify({"message": "Company name is required for login"}), 400
    if not email or not password:
        return jsonify({"message": "Email and password are required"}), 400

    try:
        conn = get_db_connection(company_name) # Connect to specific company's DB
        cursor = conn.cursor()
        # MODIFIED: Query by email
        cursor.execute("SELECT id, email, password_hash, role FROM Users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user and user['password_hash'] == _hash_password(password):
            # In a real app, you'd generate a JWT token here
            return jsonify({
                "message": "Login successful!", 
                "user_id": user['id'], 
                "role": user['role'], 
                "company_name": company_name, # Return company name for frontend to use in subsequent requests
                "user_email": user['email'], # Return user email
                "token": "mock_token_" + str(user['id'])
            }), 200
        else:
            return jsonify({"message": "Invalid credentials or company name"}), 401
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' not found. Please check the company name."}), 404
    except Exception as e:
        print(f"Error during login: {e}")
        return jsonify({"message": f"An error occurred during login: {str(e)}"}), 500


# --- SuperAdmin Endpoint (for creating new companies/tenants) ---
@app.route('/api/superadmin/create_company', methods=['POST'])
def create_company():
    """
    SuperAdmin endpoint to create a new company (tenant) and its initial admin user.
    In a real app, this would be heavily secured.
    """
    data = request.get_json()
    company_name = data.get('company_name')
    admin_email = data.get('admin_email', 'admin@example.com') # Default admin email
    admin_password = data.get('admin_password', 'admin123') # Default admin password

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400
    if not admin_email or not admin_password:
        return jsonify({"message": "Admin email and password are required"}), 400


    try:
        db_path = get_db_path(company_name) # This will sanitize the company_name
        if os.path.exists(db_path):
            return jsonify({"message": f"Company '{company_name}' already exists."}), 409 # Conflict

        # Create the database file and its tables
        create_tables_for_tenant(db_path)
        # Add the initial admin user to this new database
        add_initial_admin_for_tenant(company_name, admin_email, admin_password)
        return jsonify({"message": f"Company '{company_name}' created successfully with admin '{admin_email}'."}), 201
    except ValueError as ve:
        return jsonify({"message": str(ve)}), 400
    except Exception as e:
        print(f"Error creating company: {e}")
        return jsonify({"message": f"Error creating company: {str(e)}"}), 500


# --- Organizations API Endpoints ---
@app.route('/api/organizations', methods=['GET'])
def get_organizations():
    """Retrieves all organizations from the database for a specific company, optionally filtered by search term."""
    company_name = request.args.get('company_name') # Get company_name from query params
    search_term = request.args.get('search', '')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        query = "SELECT id, name, industry, phone, email, address, description FROM Organizations"
        params = []
        if search_term:
            query += " WHERE name LIKE ?"
            params.append(f"%{search_term}%")
        query += " ORDER BY name"
        cursor.execute(query, params)
        orgs = cursor.fetchall()
        conn.close()
        return jsonify([dict(org) for org in orgs]), 200
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error fetching organizations: {e}")
        return jsonify({"message": f"Error fetching organizations: {str(e)}"}), 500


@app.route('/api/organizations', methods=['POST'])
def add_organization():
    """Adds a new organization to the database for a specific company."""
    data = request.get_json()
    company_name = data.get('company_name') # Get company_name from request body
    name = data.get('name')
    industry = data.get('industry')
    phone = data.get('phone')
    email = data.get('email')
    address = data.get('address')
    description = data.get('description')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400
    if not name:
        return jsonify({"message": "Organization name is required"}), 400

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Organizations (name, industry, phone, email, address, description) VALUES (?, ?, ?, ?, ?, ?)",
                       (name, industry, phone, email, address, description))
        conn.commit()
        new_org_id = cursor.lastrowid
        cursor.execute("SELECT id, name, industry, phone, email, address, description FROM Organizations WHERE id = ?", (new_org_id,))
        new_org = cursor.fetchone()
        return jsonify(dict(new_org)), 201 # 201 Created
    except sqlite3.IntegrityError:
        return jsonify({"message": "Organization with this name already exists"}), 409 # Conflict
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error adding organization: {e}")
        return jsonify({"message": f"Error adding organization: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()

# --- Contacts API Endpoints ---
@app.route('/api/contacts', methods=['GET'])
def get_contacts():
    """Retrieves contacts from the database for a specific company, optionally filtered by organization_id and search term."""
    company_name = request.args.get('company_name') # Get company_name from query params
    organization_id = request.args.get('organization_id')
    search_term = request.args.get('search', '')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        query = """
            SELECT C.id, C.organization_id, C.first_name, C.last_name, C.title, C.phone, C.email, C.notes, O.name AS organization_name
            FROM Contacts C
            LEFT JOIN Organizations O ON C.organization_id = O.id
        """
        params = []
        conditions = []

        if organization_id:
            conditions.append("C.organization_id = ?")
            params.append(organization_id)
        
        if search_term:
            search_pattern = f"%{search_term}%"
            conditions.append("(C.first_name LIKE ? OR C.last_name LIKE ? OR C.title LIKE ? OR O.name LIKE ?)")
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY C.last_name, C.first_name"
        
        cursor.execute(query, params)
        contacts = cursor.fetchall()
        conn.close()
        return jsonify([dict(contact) for contact in contacts]), 200
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error fetching contacts: {e}")
        return jsonify({"message": f"Error fetching contacts: {str(e)}"}), 500


@app.route('/api/contacts', methods=['POST'])
def add_contact():
    """Adds a new contact to the database for a specific company."""
    data = request.get_json()
    company_name = data.get('company_name') # Get company_name from request body
    organization_id = data.get('organization_id') # Can be None
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    title = data.get('title')
    phone = data.get('phone')
    email = data.get('email')
    notes = data.get('notes')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400
    if not first_name or not last_name:
        return jsonify({"message": "First name and last name are required for a contact"}), 400

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Contacts (organization_id, first_name, last_name, title, phone, email, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (organization_id, first_name, last_name, title, phone, email, notes))
        conn.commit()
        new_contact_id = cursor.lastrowid
        cursor.execute("""
            SELECT C.id, C.organization_id, C.first_name, C.last_name, C.title, C.phone, C.email, C.notes, O.name AS organization_name
            FROM Contacts C
            LEFT JOIN Organizations O ON C.organization_id = O.id
            WHERE C.id = ?
        """, (new_contact_id,))
        new_contact = cursor.fetchone()
        return jsonify(dict(new_contact)), 201 # 201 Created
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error adding contact: {e}")
        return jsonify({"message": f"Error adding contact: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()

# --- Letters API Endpoints ---
@app.route('/api/letters', methods=['GET'])
def get_letters():
    """Retrieves letters from the database for a specific company, optionally filtered by search term."""
    company_name = request.args.get('company_name') # Get company_name from query params
    search_term = request.args.get('search', '')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    try:
        conn = get_db_connection(company_name)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = """
            SELECT 
                L.id, 
                L.letter_code_persian, 
                L.type, 
                L.date_shamsi_persian, 
                L.subject, 
                L.file_path,
                O.name AS organization_name, 
                C.first_name, 
                C.last_name
            FROM Letters L
            LEFT JOIN Organizations O ON L.organization_id = O.id
            LEFT JOIN Contacts C ON L.contact_id = C.id
        """
        params = []
        conditions = []

        if search_term:
            search_pattern = f"%{search_term}%"
            conditions.append("""
                (L.letter_code_persian LIKE ? OR 
                 L.subject LIKE ? OR 
                 O.name LIKE ? OR 
                 C.first_name LIKE ? OR 
                 C.last_name LIKE ?)
            """)
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY L.date_shamsi_persian DESC, L.id DESC" # Order by date then ID

        cursor.execute(query, tuple(params))
        letters = cursor.fetchall()
        conn.close()
        return jsonify([dict(letter) for letter in letters]), 200
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error fetching letters: {e}")
        return jsonify({"message": f"Error fetching letters: {str(e)}"}), 500


@app.route('/api/letters/download/<int:letter_id>', methods=['GET'])
def download_letter():
    """Allows downloading a generated letter file from local storage for a specific company."""
    # Note: For download, we need the company_name to get the file_path from the correct DB
    # We'll expect company_name as a query parameter for simplicity here.
    company_name = request.args.get('company_name')
    letter_id = request.args.get('letter_id', type=int) # Get letter_id from query params

    if not company_name or not letter_id:
        return jsonify({"message": "Company name and letter ID are required"}), 400

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT file_path, letter_code_persian FROM Letters WHERE id = ?", (letter_id,))
        result = cursor.fetchone()
        conn.close()

        if result and result['file_path']:
            file_path = result['file_path']
            letter_code = result['letter_code_persian']
            
            if os.path.exists(file_path):
                directory = os.path.dirname(file_path)
                filename = os.path.basename(file_path)
                return send_from_directory(directory, filename, as_attachment=True, download_name=f"{letter_code}.docx")
            else:
                return jsonify({"message": "File not found on server"}), 404
        else:
            return jsonify({"message": "Letter or file path not found"}), 404
    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404
    except Exception as e:
        print(f"Error downloading letter: {e}")
        return jsonify({"message": f"Error downloading letter: {str(e)}"}), 500


@app.route('/api/letters/generate', methods=['POST'])
def generate_letter_api():
    """
    Generates a new letter, saves it locally, and saves its metadata to the database for a specific company.
    """
    data = request.get_json()
    company_name = data.get('company_name') # Get company_name from request body
    subject = data.get('subject')
    body = data.get('body')
    letter_type = data.get('letter_type') # e.g., 'FIN', 'HR', 'GEN'
    organization_id = data.get('organization_id')
    contact_id = data.get('contact_id')
    user_id = data.get('user_id', 1) # Get user_id from request, default to 1 for now

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400
    if not all([subject, body, letter_type]):
        return jsonify({"message": "Subject, body, and letter type are required"}), 400

    try:
        # 1. Generate letter number for the specific company
        next_seq_num, year_shamsi = generate_letter_number_for_company(company_name)
        company_abbr = "NGRR" # Placeholder, will be from company-specific settings later
        
        letter_code_persian = f"{year_shamsi}/{company_abbr}-{letter_type}-{next_seq_num:03d}"
        letter_filename_part = f"{company_abbr}-{letter_type}-{next_seq_num:03d}" # Corrected variable name
        
        # 2. Prepare DOCX content
        document = Document()
        document.add_heading(subject, level=1)
        document.add_paragraph(body)
        document.add_paragraph(f"شماره نامه: {convert_numbers_to_persian(letter_code_persian)}")
        document.add_paragraph(f"تاریخ: {convert_numbers_to_persian(jdatetime.date.today().strftime('%Y/%m/%d'))}")

        # 3. Save to local file system within the company's generated letters directory
        # Company-specific directory for generated letters
        company_letters_dir = os.path.join(GENERATED_LETTERS_BASE_DIR, company_name)
        year_dir = os.path.join(company_letters_dir, str(year_shamsi))
        
        os.makedirs(year_dir, exist_ok=True) 

        local_file_path = os.path.join(year_dir, f"{letter_filename_part}.docx")
        document.save(local_file_path)
        # Corrected print statement variable name
        print(f"Saved {letter_filename_part}.docx for company '{company_name}' to local path: {local_file_path}")

        # 4. Save letter metadata to database for the specific company
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        current_gregorian_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        date_shamsi_persian = jdatetime.date.today().strftime('%Y/%m/%d')

        cursor.execute("""
            INSERT INTO Letters (
                letter_code_prefix, letter_code_number, letter_code_persian, type, 
                date_shamsi_persian, subject, body, organization_id, contact_id, 
                file_path, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_abbr, next_seq_num, letter_code_persian, letter_type,
            date_shamsi_persian, subject, body, organization_id, contact_id,
            local_file_path, current_gregorian_date, user_id
        ))
        conn.commit()

        return jsonify({"message": "Letter generated and saved successfully", "letter_code": letter_code_persian, "letter_id": cursor.lastrowid}), 201

    except FileNotFoundError:
        return jsonify({"message": f"Company '{company_name}' database not found. Cannot generate letter."}), 404
    except Exception as e:
        print(f"Error generating or saving letter: {e}")
        return jsonify({"message": f"Error generating or saving letter: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()


# --- Main execution block ---
if __name__ == '__main__':
    # Ensure 'templates' and 'data' directories exist
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('data'):
        os.makedirs('data')
    if not os.path.exists(TENANT_DB_BASE_DIR):
        os.makedirs(TENANT_DB_BASE_DIR)
    if not os.path.exists(GENERATED_LETTERS_BASE_DIR):
        os.makedirs(GENERATED_LETTERS_BASE_DIR)
    
    print("Flask app starting...")
    # Run the Flask app. DO NOT use debug=True in production.
    # host='0.0.0.0' makes the server accessible from other machines on the network.
    app.run(debug=True, host='0.0.0.0', port=5000)
