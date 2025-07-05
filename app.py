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
from docx.shared import Inches # For potentially adding images, though we'll focus on text for now
from docx.enum.text import WD_ALIGN_PARAGRAPH # For text alignment in DOCX
import urllib.parse # For encoding filenames in download

# Initialize the Flask application
app = Flask(__name__)
CORS(app) # Enable CORS for all routes, allowing frontend to access backend

# --- Configuration ---
# Base directory for all tenant databases
TENANT_DB_BASE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'data', 'tenants')
# This path will be used to store generated DOCX files on the server locally.
GENERATED_LETTERS_BASE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'generated_letters')
# Directory to store uploaded DOCX templates for each company
COMPANY_TEMPLATES_BASE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'company_templates')


SECRET_KEY = os.environ.get('SECRET_KEY', 'a_very_secret_key_for_development') 
app.config['SECRET_KEY'] = SECRET_KEY

# Ensure necessary directories exist at startup
os.makedirs(TENANT_DB_BASE_DIR, exist_ok=True)
os.makedirs(GENERATED_LETTERS_BASE_DIR, exist_ok=True) 
os.makedirs(COMPANY_TEMPLATES_BASE_DIR, exist_ok=True) # Ensure templates directory exists

# --- Database Functions ---

def get_db_path(company_name):
    """Constructs the database path for a given company."""
    db_dir = os.path.join(TENANT_DB_BASE_DIR, company_name)
    os.makedirs(db_dir, exist_ok=True) # Ensure company-specific directory exists
    return os.path.join(db_dir, f'{company_name}.db')

def init_db(company_name):
    """Initializes the database schema for a new company."""
    db_path = get_db_path(company_name)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' -- 'user', 'admin', 'superadmin'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            industry TEXT,
            phone TEXT,
            email TEXT,
            address TEXT,
            description TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            title TEXT,
            phone TEXT,
            email TEXT,
            notes TEXT,
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_abbr TEXT NOT NULL,
            seq_num INTEGER NOT NULL,
            letter_code_persian TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            date_shamsi_persian TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            organization_id INTEGER,
            contact_id INTEGER,
            local_file_path TEXT, -- Path to the generated DOCX file on the server
            current_gregorian_date TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL,
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    # New table for company-specific settings, including template path
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS company_settings (
            company_name TEXT PRIMARY KEY,
            company_short_name TEXT,
            company_full_name_footer TEXT,
            letter_template_path TEXT -- Path to the DOCX template file
        )
    ''')
    conn.commit()
    conn.close()

def get_db_connection(company_name):
    """Establishes a database connection for a given company."""
    db_path = get_db_path(company_name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row # This allows accessing columns by name
    return conn

# --- Helper function for placeholder replacement ---
def replace_placeholder_in_paragraph(paragraph, placeholder, replacement):
    """
    Replaces a placeholder in a paragraph, even if it spans multiple runs.
    This is a more robust replacement mechanism for python-docx.
    """
    full_text = "".join(run.text for run in paragraph.runs)
    if placeholder in full_text:
        new_text = full_text.replace(placeholder, replacement)
        paragraph.clear() # Clear existing runs
        paragraph.add_run(new_text) # Add new run with replaced text
        return True
    return False

# --- Routes for serving HTML files ---
@app.route('/')
def serve_login_page():
    """Serves the login.html page."""
    return render_template('login.html')

@app.route('/main_app')
def serve_main_app():
    """Serves the index.html (main application) page."""
    return render_template('index.html')


# --- User/Authentication Endpoints ---

@app.route('/api/superadmin/create_company', methods=['POST'])
def create_company():
    """Superadmin endpoint to create a new company and its admin user."""
    data = request.get_json()
    company_name = data.get('company_name')
    admin_email = data.get('admin_email')
    admin_password = data.get('admin_password')

    if not all([company_name, admin_email, admin_password]):
        return jsonify({"message": "Company name, admin email, and password are required"}), 400

    # Check if company already exists (by checking if its DB exists)
    if os.path.exists(get_db_path(company_name)):
        return jsonify({"message": f"Company '{company_name}' already exists"}), 409

    try:
        init_db(company_name)
        conn = sqlite3.connect(get_db_path(company_name))
        cursor = conn.cursor()
        password_hash = hashlib.sha256(admin_password.encode()).hexdigest()
        cursor.execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
                       (admin_email, password_hash, 'admin'))
        
        # Initialize company settings with default values
        cursor.execute("INSERT INTO company_settings (company_name, company_short_name, company_full_name_footer, letter_template_path) VALUES (?, ?, ?, ?)",
                       (company_name, company_name, f"شرکت {company_name}", None)) # Default template path is None
        
        conn.commit()
        conn.close()
        return jsonify({"message": f"Company '{company_name}' created with admin user '{admin_email}'"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"message": "Admin user with this email already exists for this company"}), 409
    except Exception as e:
        print(f"Error creating company: {e}")
        return jsonify({"message": f"Error creating company: {str(e)}"}), 500

@app.route('/api/login', methods=['POST'])
def login():
    """Handles user login."""
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    company_name = data.get('company_name')

    if not all([email, password, company_name]):
        return jsonify({"message": "Email, password, and company name are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, password_hash, role FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user and hashlib.sha256(password.encode()).hexdigest() == user['password_hash']:
            # Fetch company settings
            conn_settings = get_db_connection(company_name)
            cursor_settings = conn_settings.cursor()
            cursor_settings.execute("SELECT company_short_name, company_full_name_footer, letter_template_path FROM company_settings WHERE company_name = ?", (company_name,))
            settings = cursor_settings.fetchone()
            conn_settings.close()

            user_data = {
                "user_id": user['id'],
                "user_email": user['email'],
                "role": user['role'],
                "company_name": company_name,
                "company_short_name": settings['company_short_name'] if settings else company_name,
                "company_full_name_footer": settings['company_full_name_footer'] if settings else f"شرکت {company_name}",
                "letter_template_path": settings['letter_template_path'] if settings else None # NEW: Include template path
            }
            return jsonify(user_data), 200
        else:
            return jsonify({"message": "Invalid credentials"}), 401
    except Exception as e:
        print(f"Error during login: {e}")
        return jsonify({"message": f"Error during login: {str(e)}"}), 500

# --- User Management (Admin only) ---
@app.route('/api/users', methods=['POST'])
def add_user():
    """Adds a new user to a company (Admin/Superadmin only)."""
    data = request.get_json()
    company_name = data.get('company_name')
    email = data.get('email')
    password = data.get('password')
    role = data.get('role', 'user') # Default role is 'user'

    if not all([company_name, email, password]):
        return jsonify({"message": "Company name, email, and password are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute("INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)",
                       (email, password_hash, role))
        conn.commit()
        new_user_id = cursor.lastrowid
        conn.close()
        return jsonify({"message": "User added successfully", "user_id": new_user_id, "email": email, "role": role}), 201
    except sqlite3.IntegrityError:
        return jsonify({"message": "User with this email already exists in this company"}), 409
    except Exception as e:
        print(f"Error adding user: {e}")
        return jsonify({"message": f"Error adding user: {str(e)}"}), 500

@app.route('/api/users', methods=['GET'])
def get_users():
    """Retrieves all users for a company (Admin/Superadmin only)."""
    company_name = request.args.get('company_name')
    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, role FROM users")
        users = cursor.fetchall()
        conn.close()
        return jsonify([dict(user) for user in users]), 200
    except Exception as e:
        print(f"Error fetching users: {e}")
        return jsonify({"message": f"Error fetching users: {str(e)}"}), 500

@app.route('/api/users/<int:user_id>', methods=['PUT'])
def update_user_role(user_id):
    """Updates a user's role (Admin/Superadmin only)."""
    data = request.get_json()
    company_name = data.get('company_name')
    new_role = data.get('role')

    if not all([company_name, new_role]):
        return jsonify({"message": "Company name and new role are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "User not found"}), 404
        conn.close()
        return jsonify({"message": "User role updated successfully"}), 200
    except Exception as e:
        print(f"Error updating user role: {e}")
        return jsonify({"message": f"Error updating user role: {str(e)}"}), 500

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """Deletes a user (Admin/Superadmin only)."""
    company_name = request.args.get('company_name')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "User not found"}), 404
        conn.close()
        return jsonify({"message": "User deleted successfully"}), 200
    except Exception as e:
        print(f"Error deleting user: {e}")
        return jsonify({"message": f"Error deleting user: {str(e)}"}), 500


# --- Settings Endpoints (for company-wide settings) ---

@app.route('/api/settings', methods=['GET'])
def get_company_settings():
    """Retrieves company-specific settings."""
    company_name = request.args.get('company_name')
    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT company_short_name, company_full_name_footer, letter_template_path FROM company_settings WHERE company_name = ?", (company_name,))
        settings = cursor.fetchone()
        conn.close()
        if settings:
            return jsonify(dict(settings)), 200
        else:
            # If no settings found, return defaults
            return jsonify({
                "company_short_name": company_name,
                "company_full_name_footer": f"شرکت {company_name}",
                "letter_template_path": None
            }), 200
    except Exception as e:
        print(f"Error fetching company settings: {e}")
        return jsonify({"message": f"Error fetching company settings: {str(e)}"}), 500

@app.route('/api/settings', methods=['POST'])
def update_company_settings():
    """Updates company-specific settings."""
    data = request.get_json()
    company_name = data.get('company_name')
    company_short_name = data.get('company_short_name')
    company_full_name_footer = data.get('company_full_name_footer')
    # letter_template_path is updated via a separate upload endpoint

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        # UPSERT logic: Try to update, if no rows affected, insert
        cursor.execute("UPDATE company_settings SET company_short_name = ?, company_full_name_footer = ? WHERE company_name = ?",
                       (company_short_name, company_full_name_footer, company_name))
        if cursor.rowcount == 0:
            cursor.execute("INSERT INTO company_settings (company_name, company_short_name, company_full_name_footer, letter_template_path) VALUES (?, ?, ?, ?)",
                           (company_name, company_short_name, company_full_name_footer, None)) # Template path remains None on initial insert
        conn.commit()
        conn.close()
        return jsonify({"message": "Company settings updated successfully"}), 200
    except Exception as e:
        print(f"Error updating company settings: {e}")
        return jsonify({"message": f"Error updating company settings: {str(e)}"}), 500

@app.route('/api/settings/upload_template', methods=['POST'])
def upload_letter_template():
    """
    Handles uploading a DOCX letter template for a company.
    This replaces the previous 'upload_header' endpoint.
    """
    company_name = request.form.get('company_name')
    if 'letter_template' not in request.files:
        return jsonify({"message": "No letter_template file part"}), 400

    file = request.files['letter_template']
    if file.filename == '':
        return jsonify({"message": "No selected file"}), 400

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    if not file.filename.lower().endswith('.docx'):
        return jsonify({"message": "Invalid file type. Only .docx files are allowed."}), 400

    # Create company-specific template directory
    company_template_dir = os.path.join(COMPANY_TEMPLATES_BASE_DIR, company_name)
    os.makedirs(company_template_dir, exist_ok=True)

    # Sanitize filename to prevent path traversal issues
    filename = os.path.basename(file.filename)
    template_path = os.path.join(company_template_dir, filename)

    try:
        file.save(template_path)

        # Update the letter_template_path in company_settings
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("UPDATE company_settings SET letter_template_path = ? WHERE company_name = ?",
                       (template_path, company_name))
        if cursor.rowcount == 0:
            # If no settings exist, insert them
            cursor.execute("INSERT INTO company_settings (company_name, letter_template_path) VALUES (?, ?)",
                           (company_name, template_path))
        conn.commit()
        conn.close()

        return jsonify({
            "message": "Letter template uploaded and saved successfully",
            "template_filename": filename,
            "template_full_path": template_path
        }), 200
    except Exception as e:
        print(f"Error uploading letter template: {e}")
        return jsonify({"message": f"Error uploading letter template: {str(e)}"}), 500


# --- Organization Endpoints ---

@app.route('/api/organizations', methods=['POST'])
def add_organization():
    """Adds a new organization for a company."""
    data = request.get_json()
    company_name = data.get('company_name')
    name = data.get('name')
    industry = data.get('industry')
    phone = data.get('phone')
    email = data.get('email')
    address = data.get('address')
    description = data.get('description')

    if not all([company_name, name]):
        return jsonify({"message": "Company name and organization name are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO organizations (name, industry, phone, email, address, description) VALUES (?, ?, ?, ?, ?, ?)",
                       (name, industry, phone, email, address, description))
        conn.commit()
        new_org_id = cursor.lastrowid
        conn.close()
        return jsonify({"message": "Organization added successfully", "id": new_org_id, "name": name}), 201
    except sqlite3.IntegrityError:
        return jsonify({"message": "Organization with this name already exists"}), 409
    except Exception as e:
        print(f"Error adding organization: {e}")
        return jsonify({"message": f"Error adding organization: {str(e)}"}), 500

@app.route('/api/organizations', methods=['GET'])
def get_organizations():
    """Retrieves organizations for a company, with optional search."""
    company_name = request.args.get('company_name')
    search_term = request.args.get('search', '')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        query = "SELECT id, name, industry, phone, email, address, description FROM organizations"
        params = []
        if search_term:
            query += " WHERE name LIKE ?"
            params.append(f'%{search_term}%')
        
        cursor.execute(query, params)
        organizations = cursor.fetchall()
        conn.close()
        return jsonify([dict(org) for org in organizations]), 200
    except Exception as e:
        print(f"Error fetching organizations: {e}")
        return jsonify({"message": f"Error fetching organizations: {str(e)}"}), 500

@app.route('/api/organizations/<int:org_id>', methods=['GET'])
def get_organization(org_id):
    """Retrieves a single organization by ID."""
    company_name = request.args.get('company_name')
    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, industry, phone, email, address, description FROM organizations WHERE id = ?", (org_id,))
        org = cursor.fetchone()
        conn.close()
        if org:
            return jsonify(dict(org)), 200
        else:
            return jsonify({"message": "Organization not found"}), 404
    except Exception as e:
        print(f"Error fetching organization: {e}")
        return jsonify({"message": f"Error fetching organization: {str(e)}"}), 500

@app.route('/api/organizations/<int:org_id>', methods=['PUT'])
def update_organization(org_id):
    """Updates an existing organization."""
    data = request.get_json()
    company_name = data.get('company_name')
    name = data.get('name')
    industry = data.get('industry')
    phone = data.get('phone')
    email = data.get('email')
    address = data.get('address')
    description = data.get('description')

    if not all([company_name, name]):
        return jsonify({"message": "Company name and organization name are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("UPDATE organizations SET name = ?, industry = ?, phone = ?, email = ?, address = ?, description = ? WHERE id = ?",
                       (name, industry, phone, email, address, description, org_id))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "Organization not found"}), 404
        conn.close()
        return jsonify({"message": "Organization updated successfully", "id": org_id, "name": name}), 200
    except sqlite3.IntegrityError:
        return jsonify({"message": "Organization with this name already exists"}), 409
    except Exception as e:
        print(f"Error updating organization: {e}")
        return jsonify({"message": f"Error updating organization: {str(e)}"}), 500

@app.route('/api/organizations/<int:org_id>', methods=['DELETE'])
def delete_organization(org_id):
    """Deletes an organization and sets related contacts' organization_id to NULL."""
    company_name = request.args.get('company_name')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        
        # Set organization_id to NULL for related contacts
        cursor.execute("UPDATE contacts SET organization_id = NULL WHERE organization_id = ?", (org_id,))
        
        cursor.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "Organization not found"}), 404
        conn.close()
        return jsonify({"message": "Organization and related contacts updated successfully"}), 200
    except Exception as e:
        print(f"Error deleting organization: {e}")
        return jsonify({"message": f"Error deleting organization: {str(e)}"}), 500


# --- Contact Endpoints ---

@app.route('/api/contacts', methods=['POST'])
def add_contact():
    """Adds a new contact for a company."""
    data = request.get_json()
    company_name = data.get('company_name')
    organization_id = data.get('organization_id')
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    title = data.get('title')
    phone = data.get('phone')
    email = data.get('email')
    notes = data.get('notes')

    if not all([company_name, first_name, last_name]):
        return jsonify({"message": "Company name, first name, and last name are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO contacts (organization_id, first_name, last_name, title, phone, email, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (organization_id, first_name, last_name, title, phone, email, notes))
        conn.commit()
        new_contact_id = cursor.lastrowid
        conn.close()
        return jsonify({"message": "Contact added successfully", "id": new_contact_id, "first_name": first_name, "last_name": last_name}), 201
    except Exception as e:
        print(f"Error adding contact: {e}")
        return jsonify({"message": f"Error adding contact: {str(e)}"}), 500

@app.route('/api/contacts', methods=['GET'])
def get_contacts():
    """Retrieves contacts for a company, with optional search and organization_id filter."""
    company_name = request.args.get('company_name')
    search_term = request.args.get('search', '')
    organization_id = request.args.get('organization_id')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        query = """
            SELECT c.id, c.first_name, c.last_name, c.title, c.phone, c.email, c.notes,
                   o.name AS organization_name
            FROM contacts c
            LEFT JOIN organizations o ON c.organization_id = o.id
        """
        conditions = [] # Initialize conditions list
        params = []
        if search_term:
            conditions.append("(c.first_name LIKE ? OR c.last_name LIKE ? OR c.title LIKE ? OR o.name LIKE ?)")
            params.extend([f'%{search_term}%', f'%{search_term}%', f'%{search_term}%', f'%{search_term}%'])
        
        if organization_id:
            conditions.append("c.organization_id = ?")
            params.append(organization_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        cursor.execute(query, params)
        contacts = cursor.fetchall()
        conn.close()
        return jsonify([dict(contact) for contact in contacts]), 200
    except Exception as e:
        print(f"Error fetching contacts: {e}")
        return jsonify({"message": f"Error fetching contacts: {str(e)}"}), 500

@app.route('/api/contacts/<int:contact_id>', methods=['GET'])
def get_contact(contact_id):
    """Retrieves a single contact by ID."""
    company_name = request.args.get('company_name')
    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.id, c.first_name, c.last_name, c.title, c.phone, c.email, c.notes, c.organization_id,
                   o.name AS organization_name
            FROM contacts c
            LEFT JOIN organizations o ON c.organization_id = o.id
            WHERE c.id = ?
        """, (contact_id,))
        contact = cursor.fetchone()
        conn.close()
        if contact:
            return jsonify(dict(contact)), 200
        else:
            return jsonify({"message": "Contact not found"}), 404
    except Exception as e:
        print(f"Error fetching contact: {e}")
        return jsonify({"message": f"Error fetching contact: {str(e)}"}), 500

@app.route('/api/contacts/<int:contact_id>', methods=['PUT'])
def update_contact(contact_id):
    """Updates an existing contact."""
    data = request.get_json()
    company_name = data.get('company_name')
    organization_id = data.get('organization_id')
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    title = data.get('title')
    phone = data.get('phone')
    email = data.get('email')
    notes = data.get('notes')

    if not all([company_name, first_name, last_name]):
        return jsonify({"message": "Company name, first name, and last name are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("UPDATE contacts SET organization_id = ?, first_name = ?, last_name = ?, title = ?, phone = ?, email = ?, notes = ? WHERE id = ?",
                       (organization_id, first_name, last_name, title, phone, email, notes, contact_id))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "Contact not found"}), 404
        conn.close()
        return jsonify({"message": "Contact updated successfully", "id": contact_id, "first_name": first_name, "last_name": last_name}), 200
    except Exception as e:
        print(f"Error updating contact: {e}")
        return jsonify({"message": f"Error updating contact: {str(e)}"}), 500

@app.route('/api/contacts/<int:contact_id>', methods=['DELETE'])
def delete_contact(contact_id):
    """Deletes a contact."""
    company_name = request.args.get('company_name')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"message": "Contact not found"}), 404
        conn.close()
        return jsonify({"message": "Contact deleted successfully"}), 200
    except Exception as e:
        print(f"Error deleting contact: {e}")
        return jsonify({"message": f"Error deleting contact: {str(e)}"}), 500


# --- Letter Endpoints ---

@app.route('/api/letters/generate', methods=['POST'])
def generate_letter():
    """
    Generates a DOCX letter using a template, fills placeholders,
    and saves it locally, then returns info for download.
    """
    data = request.get_json()
    company_name = data.get('company_name')
    subject = data.get('subject')
    body = data.get('body')
    letter_type = data.get('letter_type')
    # Ensure organization_id and contact_id are handled correctly (can be None)
    organization_id = data.get('organization_id')
    contact_id = data.get('contact_id')
    user_id = data.get('user_id') # User who generated the letter

    if not all([company_name, subject, body, letter_type, user_id]):
        return jsonify({"message": "Company name, subject, body, letter type, and user ID are required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' database not found."}), 404

    conn = None
    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()

        # 1. Fetch company settings for template path and company names
        cursor.execute("SELECT company_short_name, company_full_name_footer, letter_template_path FROM company_settings WHERE company_name = ?", (company_name,))
        settings = cursor.fetchone()
        
        letter_template_path = settings['letter_template_path'] if settings else None
        company_short_name = settings['company_short_name'] if settings else company_name
        company_full_name_footer = settings['company_full_name_footer'] if settings else f"شرکت {company_name}"

        if not letter_template_path or not os.path.exists(letter_template_path):
            return jsonify({"message": "No letter template configured or template file not found. Please upload a .docx template in settings."}), 400

        # 2. Get next sequence number for letter code
        cursor.execute("SELECT MAX(seq_num) FROM letters WHERE company_abbr = ?", (company_short_name,))
        max_seq_num = cursor.fetchone()[0]
        next_seq_num = (max_seq_num if max_seq_num is not None else 0) + 1

        # 3. Generate Persian date and Gregorian date
        current_gregorian_date = datetime.now()
        date_shamsi_persian = jdatetime.date.fromgregorian(date=current_gregorian_date).strftime("%Y/%m/%d")

        # 4. Construct letter code
        letter_code_persian = f"{company_short_name}-{next_seq_num:04d}-{letter_type}-{date_shamsi_persian.replace('/', '')}"

        # 5. Fetch organization and contact details if provided
        organization_name = ""
        contact_name = ""
        if organization_id:
            cursor.execute("SELECT name FROM organizations WHERE id = ?", (organization_id,))
            org = cursor.fetchone()
            if org:
                organization_name = org['name']
        
        if contact_id:
            cursor.execute("SELECT first_name, last_name FROM contacts WHERE id = ?", (contact_id,))
            contact = cursor.fetchone()
            if contact:
                contact_name = f"{contact['first_name']} {contact['last_name']}"

        # 6. Load DOCX template and replace placeholders using the robust function
        doc = Document(letter_template_path)

        # Iterate through paragraphs and replace text
        for paragraph in doc.paragraphs:
            replace_placeholder_in_paragraph(paragraph, '[[DATE]]', date_shamsi_persian)
            replace_placeholder_in_paragraph(paragraph, '[[CODE]]', letter_code_persian)
            replace_placeholder_in_paragraph(paragraph, '[[ORGANIZATION_NAME]]', organization_name)
            replace_placeholder_in_paragraph(paragraph, '[[CONTACT_NAME]]', contact_name)
            replace_placeholder_in_paragraph(paragraph, '[[SUBJECT]]', subject)
            replace_placeholder_in_paragraph(paragraph, '[[BODY]]', body)
            replace_placeholder_in_paragraph(paragraph, '[[COMPANY_NAME]]', company_full_name_footer) # Use full name for footer

        # Iterate through tables (if any) and replace text
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        replace_placeholder_in_paragraph(paragraph, '[[DATE]]', date_shamsi_persian)
                        replace_placeholder_in_paragraph(paragraph, '[[CODE]]', letter_code_persian)
                        replace_placeholder_in_paragraph(paragraph, '[[ORGANIZATION_NAME]]', organization_name)
                        replace_placeholder_in_paragraph(paragraph, '[[CONTACT_NAME]]', contact_name)
                        replace_placeholder_in_paragraph(paragraph, '[[SUBJECT]]', subject)
                        replace_placeholder_in_paragraph(paragraph, '[[BODY]]', body)
                        replace_placeholder_in_paragraph(paragraph, '[[COMPANY_NAME]]', company_full_name_footer)


        # 7. Save the generated DOCX file
        generated_letters_company_dir = os.path.join(GENERATED_LETTERS_BASE_DIR, company_name)
        os.makedirs(generated_letters_company_dir, exist_ok=True)
        
        output_filename = f"{letter_code_persian}.docx"
        local_file_path = os.path.join(generated_letters_company_dir, output_filename)
        doc.save(local_file_path)

        # 8. Save letter metadata to database
        cursor.execute("""
            INSERT INTO letters (company_abbr, seq_num, letter_code_persian, type, date_shamsi_persian, subject, body, organization_id, contact_id, local_file_path, current_gregorian_date, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_short_name, next_seq_num, letter_code_persian, letter_type,
            date_shamsi_persian, subject, body, organization_id, contact_id,
            local_file_path, current_gregorian_date.strftime("%Y-%m-%d %H:%M:%S"), user_id
        ))
        conn.commit()

        return jsonify({
            "message": "Letter generated and saved successfully",
            "letter_code": letter_code_persian,
            "letter_id": cursor.lastrowid,
            "download_url": f"/api/letters/download/{cursor.lastrowid}?company_name={company_name}"
        }), 201

    except FileNotFoundError:
        return jsonify({"message": f"Letter template not found at {letter_template_path}. Please check settings."}), 404
    except Exception as e:
        print(f"Error generating or saving letter: {e}")
        return jsonify({"message": f"Error generating or saving letter: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@app.route('/api/letters', methods=['GET'])
def get_letters():
    """Retrieves letters for a company, with optional search."""
    company_name = request.args.get('company_name')
    search_term = request.args.get('search', '')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        query = """
            SELECT l.id, l.letter_code_persian, l.type, l.date_shamsi_persian, l.subject, l.body,
                   o.name AS organization_name, c.first_name, c.last_name
            FROM letters l
            LEFT JOIN organizations o ON l.organization_id = o.id
            LEFT JOIN contacts c ON l.contact_id = c.id
        """
        params = []
        if search_term:
            query += " WHERE l.letter_code_persian LIKE ? OR l.subject LIKE ? OR o.name LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ?"
            params.extend([f'%{search_term}%', f'%{search_term}%', f'%{search_term}%', f'%{search_term}%', f'%{search_term}%'])
        
        cursor.execute(query, params)
        letters = cursor.fetchall()
        conn.close()
        return jsonify([dict(letter) for letter in letters]), 200
    except Exception as e:
        print(f"Error fetching letters: {e}")
        return jsonify({"message": f"Error fetching letters: {str(e)}"}), 500

@app.route('/api/letters/download/<int:letter_id>', methods=['GET'])
def download_letter(letter_id):
    """Serves the generated DOCX letter file for download."""
    company_name = request.args.get('company_name')

    if not company_name:
        return jsonify({"message": "Company name is required"}), 400

    db_path = get_db_path(company_name)
    if not os.path.exists(db_path):
        return jsonify({"message": f"Company '{company_name}' not found"}), 404

    conn = None
    try:
        conn = get_db_connection(company_name)
        cursor = conn.cursor()
        cursor.execute("SELECT local_file_path, letter_code_persian FROM letters WHERE id = ?", (letter_id,))
        letter = cursor.fetchone()
        conn.close()

        if not letter or not letter['local_file_path'] or not os.path.exists(letter['local_file_path']):
            return jsonify({"message": "Letter file not found on server"}), 404
        
        directory = os.path.dirname(letter['local_file_path'])
        original_filename = os.path.basename(letter['local_file_path'])

        # Encode filename for Content-Disposition header
        # Use 'utf-8' for filename and then quote it for URL safety
        encoded_filename = urllib.parse.quote(original_filename.encode('utf-8'))
        
        # Create a Flask Response object and manually set Content-Disposition
        response = send_from_directory(directory, original_filename, as_attachment=True)
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response

    except Exception as e:
        print(f"Error serving letter file: {e}")
        return jsonify({"message": f"Error serving letter file: {str(e)}"}), 500
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
    if not os.path.exists(COMPANY_TEMPLATES_BASE_DIR):
        os.makedirs(COMPANY_TEMPLATES_BASE_DIR)

    # You might want to run this once for initial setup or use a separate script
    # init_db('default_company') 
    
    app.run(debug=True, port=5000)
