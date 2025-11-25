import os
import json
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson.objectid import ObjectId # To handle MongoDB's _id

# --- MongoDB Configuration ---
# NOTE: Ensure a local MongoDB instance is running on the default port 27017
MONGO_URI = 'mongodb+srv://sangitabiswas841:MYmaa1998@cluster0.1uxaqas.mongodb.net/'
DATABASE_NAME = 'virtual_boardroom'

try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client[DATABASE_NAME]
    
    # Initialize Collections (Publicly accessible)
    users_collection = db['users']
    teams_collection = db['teams']
    attendance_logs_collection = db['attendance_logs']
    attendance_records_collection = db['attendance_records']
    breaks_collection = db['breaks']
    kanban_collection = db['kanban_cards']
    whiteboard_collection = db['whiteboard']
    time_tracking_collection = db['time_entries']
    document_collection = db['document']
    document_comments_collection = db['document_comments']
    calendar_events_collection = db['calendar_events']
    polls_collection = db['polls']
    
    # Ensure indexes for performance (optional but recommended)
    users_collection.create_index("username", unique=True, sparse=True)
    users_collection.create_index("email", unique=True, sparse=True)
    attendance_records_collection.create_index([("userId", 1), ("date", 1)], unique=True)
    
    print(f"Successfully connected to MongoDB database: {DATABASE_NAME}")

except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    # In a production app, this would crash the application setup
    # For this example, we continue but all DB operations will fail
    pass 

# --- App Initialization ---
app = Flask(__name__)
app.secret_key = 'your-super-secret-key-change-in-production'
CORS(app)
# Increased max_http_buffer_size for larger payloads (e.g., whiteboard/recordings)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', max_http_buffer_size=10*1024*1024) 

# --- Data Storage (Active Users only, persistence is now MongoDB) ---
active_users = {}

# Utility function to convert MongoDB cursor results to JSON-serializable list
def to_json_serializable(data):
    if isinstance(data, list):
        return [to_json_serializable(item) for item in data]
    if isinstance(data, dict):
        # Convert ObjectId to string and remove keys if necessary
        if '_id' in data and isinstance(data['_id'], ObjectId):
            data['_id'] = str(data['_id'])
        return data
    return data

# --- Main Route ---
@app.route('/')
def index():
    return render_template('index.html')

# ==================== HELPER FUNCTIONS (MongoDB Implemented) ====================

def get_user_id(username):
    """Get user ID from username using MongoDB (case-insensitive find)."""
    user = users_collection.find_one({'username': {'$regex': f'^{username}$', '$options': 'i'}})
    return user.get('id') if user else None

def get_user_by_username(username):
    """Get user object from username using MongoDB (case-insensitive find)."""
    return users_collection.find_one({'username': {'$regex': f'^{username}$', '$options': 'i'}})

def create_or_update_attendance_record(username, timestamp, status, location, work_mode, notes):
    """Create or update attendance record for check-in/out in MongoDB."""
    user = get_user_by_username(username)
    if not user: return None
    
    record_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    user_id = user.get('id')
    
    filter_query = {'userId': user_id, 'date': record_date}
    
    update_data = {
        '$set': {
            'username': username,
            'department': user.get('department', 'Unknown'),
            'status': status,
            'checkIn': datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%H:%M:%S'),
            'location': location,
            'workMode': work_mode,
            'notes': notes,
            'lastUpdated': datetime.now().isoformat()
        },
        '$setOnInsert': {
            'id': f"{user_id}-{record_date}", 
            'userId': user_id,
            'date': record_date,
            'hoursWorked': 0,
            'checkOut': None
        }
    }
    
    attendance_records_collection.update_one(filter_query, update_data, upsert=True)
    return attendance_records_collection.find_one(filter_query)

def update_attendance_checkout(username, timestamp, work_hours):
    """Update attendance record with check-out time in MongoDB."""
    user = get_user_by_username(username)
    if not user: return None
    
    record_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    user_id = user.get('id')

    filter_query = {'userId': user_id, 'date': record_date}
    
    update_data = {
        '$set': {
            'checkOut': datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%H:%M:%S'),
            'hoursWorked': work_hours,
            'lastUpdated': datetime.now().isoformat()
        }
    }
    
    attendance_records_collection.update_one(filter_query, update_data)
    return attendance_records_collection.find_one(filter_query)

def get_filtered_attendance_data(start_date, end_date, department='all'):
    """Get attendance data filtered by date range and department using MongoDB."""
    query = {
        'date': {'$gte': start_date, '$lte': end_date}
    }
    if department != 'all':
        query['department'] = department
    
    records_cursor = attendance_records_collection.find(query).sort([('date', 1), ('username', 1)])
    return to_json_serializable(list(records_cursor))

def calculate_attendance_summary_stats(attendance_data, start_date, end_date):
    """Calculate summary statistics for attendance data."""
    all_users_count = users_collection.count_documents({})
    if not attendance_data:
        return {
            'totalEmployees': all_users_count, 'presentToday': 0, 'absentToday': all_users_count, 
            'avgAttendance': '0%', 'totalWorkHours': 0, 'avgWorkHours': 0
        }
    
    unique_employees = list(set([r['username'] for r in attendance_data]))
    today = datetime.now().strftime('%Y-%m-%d')
    today_records = [r for r in attendance_data if r['date'] == today]
    present_today = len([r for r in today_records if r.get('status') == 'Present'])
    absent_today = all_users_count - present_today
    
    total_records = len(attendance_data)
    present_records = len([r for r in attendance_data if r.get('status') == 'Present'])
    avg_attendance = round((present_records / total_records) * 100) if total_records > 0 else 0
    
    total_work_hours = sum(r.get('hoursWorked', 0) for r in attendance_data)
    avg_work_hours = round(total_work_hours / len(unique_employees), 2) if unique_employees else 0
    
    return {
        'totalEmployees': all_users_count, 'presentToday': present_today, 'absentToday': absent_today,
        'avgAttendance': f'{avg_attendance}%', 'totalWorkHours': total_work_hours, 'avgWorkHours': avg_work_hours
    }

def generate_attendance_analytics(attendance_data):
    """Generate analytics data for charts (Updated breakPatterns)"""
    if not attendance_data:
        return {
            'departmentRates': [], 'dailyTrend': [], 'punctuality': [], 'breakPatterns': []
        }
    
    # Break patterns (UPDATED to query MongoDB)
    break_counts = list(breaks_collection.aggregate([
        {'$group': {'_id': '$type', 'count': {'$sum': 1}}}
    ]))
    break_patterns = [{'type': item['_id'].capitalize() + ' Break', 'count': item['count']} for item in break_counts]
    
    # ... (Other analytics functions remain the same as they operate on the fetched attendance_data)
    
    # Department-wise attendance rates
    departments = list(set([r['department'] for r in attendance_data]))
    department_rates = []
    for dept in departments:
        dept_records = [r for r in attendance_data if r['department'] == dept]
        present_count = len([r for r in dept_records if r['status'] == 'Present'])
        total_count = len(dept_records)
        attendance_rate = round((present_count / total_count) * 100) if total_count > 0 else 0
        department_rates.append({'department': dept, 'attendanceRate': attendance_rate, 'totalEmployees': len(set([r['username'] for r in dept_records]))})

    # Daily trend (last 7 days)
    daily_trend = []
    for i in range(6, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        day_records = [r for r in attendance_data if r['date'] == date]
        present_count = len([r for r in day_records if r['status'] == 'Present'])
        total_count = len(set([r['username'] for r in day_records]))
        attendance_rate = round((present_count / total_count) * 100) if total_count > 0 else 0
        daily_trend.append({'date': date, 'attendanceRate': attendance_rate, 'presentCount': present_count, 'totalCount': total_count})
        
    # Punctuality analysis
    def is_on_time(check_in_time):
        try:
            check_in = datetime.strptime(check_in_time, '%H:%M:%S')
            return check_in.hour < 9 or (check_in.hour == 9 and check_in.minute <= 30)
        except: return False
        
    punctuality_data = []
    for username in set([r['username'] for r in attendance_data]):
        user_records = [r for r in attendance_data if r['username'] == username and r.get('checkIn')]
        if user_records:
            on_time_count = len([r for r in user_records if is_on_time(r['checkIn'])])
            punctuality_rate = round((on_time_count / len(user_records)) * 100) if user_records else 0
            punctuality_data.append({'username': username, 'punctualityRate': punctuality_rate, 'totalRecords': len(user_records)})

    return {
        'departmentRates': department_rates,
        'dailyTrend': daily_trend,
        'punctuality': punctuality_data[:10],
        'breakPatterns': break_patterns
    }

def get_employee_statistics(attendance_data):
    """Get employee-specific statistics (no DB change needed, operates on fetched data)"""
    employee_stats = {}
    
    for record in attendance_data:
        username = record['username']
        if username not in employee_stats:
            employee_stats[username] = {'presentCount': 0, 'totalRecords': 0, 'totalHours': 0, 'overtimeHours': 0}
        
        employee_stats[username]['totalRecords'] += 1
        if record.get('status') == 'Present': employee_stats[username]['presentCount'] += 1
        
        hours_worked = record.get('hoursWorked', 0)
        employee_stats[username]['totalHours'] += hours_worked
        if hours_worked > 8: employee_stats[username]['overtimeHours'] += (hours_worked - 8)
    
    if employee_stats:
        most_punctual = max(employee_stats.items(), key=lambda x: (x[1]['presentCount'] / x[1]['totalRecords']) if x[1]['totalRecords'] > 0 else 0)
        most_absences = min(employee_stats.items(), key=lambda x: (x[1]['presentCount'] / x[1]['totalRecords']) if x[1]['totalRecords'] > 0 else 1)
        avg_work_hours = sum(stats['totalHours'] for stats in employee_stats.values()) / len(employee_stats)
        total_overtime = sum(stats['overtimeHours'] for stats in employee_stats.values())
        
        return {
            'mostPunctual': f"{most_punctual[0]} ({round((most_punctual[1]['presentCount'] / most_punctual[1]['totalRecords']) * 100)}%)",
            'mostAbsences': f"{most_absences[0]} ({round((most_absences[1]['presentCount'] / most_absences[1]['totalRecords']) * 100)}%)",
            'avgWorkHours': f"{avg_work_hours:.1f}h",
            'totalOvertime': f"{total_overtime:.1f}h"
        }
    
    return {'mostPunctual': '-', 'mostAbsences': '-', 'avgWorkHours': '-', 'totalOvertime': '-'}


# --- Dashboard API (Updated for MongoDB) ---
def get_dashboard_data():
    """Helper function to aggregate all dashboard statistics using MongoDB."""
    
    all_teams = list(teams_collection.find({}))
    all_users = list(users_collection.find({}))
    
    total_projects = sum(len(team.get('projects', [])) for team in all_teams)
    departments = ["Engineering", "Marketing", "Sales", "HR", "Finance", "Operations", 
                   "Customer Support", "Product Management", "Design", "Legal"]
    department_breakdown = {dept: {'total': 0, 'active': 0} for dept in departments}

    for user in all_users:
        dept = user.get('department')
        if dept in department_breakdown:
            department_breakdown[dept]['total'] += 1
            if user['username'] in active_users:
                department_breakdown[dept]['active'] += 1
    
    all_users_with_status = sorted([
        {**user, 'password_hash': '', 'isActive': user['username'] in active_users, '_id': str(user['_id'])}
        for user in all_users
    ], key=lambda x: x['username'].lower())
    
    return {
        'totalUsers': len(all_users),
        'activeNow': len(active_users),
        'totalTeams': len(all_teams),
        'totalProjects': total_projects,
        'departmentBreakdown': department_breakdown,
        'activeUsersList': sorted([{'username': u, 'department': d} for u, d in active_users.items()], 
                                  key=lambda x: x['username'].lower()),
        'allUsers': all_users_with_status
    }

@app.route('/api/dashboard_data', methods=['GET'])
def dashboard_data():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    return jsonify(get_dashboard_data())


# --- User Authentication & Session API (Updated for MongoDB) ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    email = data.get('email')
    department = data.get('department')
    password = data.get('password')
    profile_image = data.get('profileImage')

    if not all([username, email, department, password]):
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400

    if users_collection.find_one({'username': {'$regex': f'^{username}$', '$options': 'i'}}):
        return jsonify({'success': False, 'message': 'Username already exists.'}), 409
    if users_collection.find_one({'email': {'$regex': f'^{email}$', '$options': 'i'}}):
        return jsonify({'success': False, 'message': 'Email is already registered.'}), 409

    new_user = {
        'id': int(datetime.now().timestamp() * 1000),
        'username': username,
        'email': email,
        'department': department,
        'password_hash': generate_password_hash(password),
        'profile_image': profile_image,
        'registered_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    users_collection.insert_one(new_user)
    
    return jsonify({'success': True, 'message': 'Registration successful! Please log in.'}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    user = users_collection.find_one({'username': {'$regex': f'^{username}$', '$options': 'i'}})

    if user and check_password_hash(user['password_hash'], password):
        session['username'] = user['username']
        active_users[user['username']] = user['department']
        socketio.emit('update-dashboard', get_dashboard_data())
        return jsonify({
            'success': True, 
            'username': user['username'],
            'profileImage': user.get('profile_image')
        })
    
    return jsonify({'success': False, 'message': 'Invalid username or password.'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    username = session.pop('username', None)
    if username and username in active_users:
        del active_users[username]
        socketio.emit('update-dashboard', get_dashboard_data())
    return jsonify({'success': True})

# ==================== ATTENDANCE DEVICE API (MongoDB Implemented) ====================

@app.route('/api/attendance/checkin', methods=['POST'])
def check_in():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    try:
        timestamp = data.get('timestamp')
        note = data.get('note', '')
        location = data.get('location', 'office_main')
        work_mode = data.get('workMode', 'full_time')
        
        user_id = get_user_id(username)
        if not user_id: return jsonify({'success': False, 'message': 'User not found'}), 404
        
        checkin_record = {
            'id': int(datetime.now().timestamp() * 1000),
            'userId': user_id,
            'username': username,
            'type': 'checkin',
            'timestamp': timestamp,
            'note': note,
            'location': location,
            'workMode': work_mode,
            'date': datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%Y-%m-%d')
        }
        
        attendance_logs_collection.insert_one(checkin_record)
        attendance_record = create_or_update_attendance_record(username, timestamp, 'Present', location, work_mode, note)
        
        socketio.emit('attendance-update', {
            'type': 'checkin', 'username': username, 'timestamp': timestamp, 'location': location
        })
        
        return jsonify({
            'success': True, 
            'record': to_json_serializable(checkin_record),
            'message': 'Checked in successfully'
        })
        
    except Exception as e:
        print(f"Error in check-in: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/attendance/checkout', methods=['POST'])
def check_out():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    try:
        timestamp = data.get('timestamp')
        note = data.get('note', '')
        total_hours = data.get('totalHours', 0) 
        work_hours = data.get('workHours', 0)
        breaks = data.get('breaks', [])
        
        user_id = get_user_id(username)
        if not user_id: return jsonify({'success': False, 'message': 'User not found'}), 404

        checkout_record = {
            'id': int(datetime.now().timestamp() * 1000), 'userId': user_id, 'username': username,
            'type': 'checkout', 'timestamp': timestamp, 'note': note, 'totalHours': total_hours, 
            'workHours': work_hours, 'breaks': breaks,
            'date': datetime.fromisoformat(timestamp.replace('Z', '+00:00')).strftime('%Y-%m-%d')
        }
        
        attendance_logs_collection.insert_one(checkout_record)
        update_attendance_checkout(username, timestamp, work_hours)
        
        socketio.emit('attendance-update', {
            'type': 'checkout', 'username': username, 'timestamp': timestamp, 'workHours': work_hours
        })
        
        return jsonify({'success': True, 'record': to_json_serializable(checkout_record), 'message': 'Checked out successfully'})
        
    except Exception as e:
        print(f"Error in check-out: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/attendance/break/start', methods=['POST'])
def start_break():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    try:
        break_type = data.get('type', 'coffee')
        planned_duration = data.get('plannedDuration', 15)
        notes = data.get('notes', '')
        user_id = get_user_id(username)

        break_record = {
            'id': int(datetime.now().timestamp() * 1000), 'userId': user_id, 'username': username,
            'type': break_type, 'startTime': datetime.now().isoformat(), 'plannedDuration': planned_duration,
            'notes': notes, 'endTime': None, 'date': datetime.now().strftime('%Y-%m-%d')
        }
        
        result = breaks_collection.insert_one(break_record)
        break_record['_id'] = str(result.inserted_id)

        socketio.emit('attendance-update', {'type': 'break_start', 'username': username, 'breakType': break_type})
        
        return jsonify({'success': True, 'breakRecord': break_record, 'message': 'Break started successfully'})
        
    except Exception as e:
        print(f"Error starting break: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/attendance/break/end', methods=['POST'])
def end_break():
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    try:
        break_record = breaks_collection.find_one_and_update(
            {'username': username, 'endTime': None},
            {'$set': {'endTime': datetime.now().isoformat()}},
            sort=[('startTime', -1)], return_document=True
        )
        
        if not break_record: return jsonify({'success': False, 'message': 'No active break found'}), 404
        
        start_time = datetime.fromisoformat(break_record['startTime'])
        end_time = datetime.fromisoformat(break_record['endTime'])
        actual_duration = int((end_time - start_time).total_seconds() / 60)
        
        breaks_collection.update_one({'_id': break_record['_id']}, {'$set': {'actualDuration': actual_duration}})
        break_record['actualDuration'] = actual_duration
        break_record['_id'] = str(break_record['_id'])
        
        socketio.emit('attendance-update', {'type': 'break_end', 'username': username, 'breakType': break_record['type'], 'duration': actual_duration})
        
        return jsonify({'success': True, 'breakRecord': break_record, 'message': f'Break ended successfully. Duration: {actual_duration} minutes'})
        
    except Exception as e:
        print(f"Error ending break: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/attendance/status', methods=['GET'])
def get_current_status():
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        user_id = get_user_id(username)
        if not user_id: return jsonify({'success': False, 'message': 'User not found'}), 404
        
        today_record = attendance_records_collection.find_one({'userId': user_id, 'date': today})
        active_break = breaks_collection.find_one({'username': username, 'endTime': None})
        breaks_today_count = breaks_collection.count_documents({'username': username, 'date': today, 'endTime': {'$ne': None}})

        if active_break: active_break['_id'] = str(active_break['_id'])
        
        status_info = {
            'checkedIn': today_record is not None and today_record.get('checkOut') is None,
            'checkInTime': today_record.get('checkIn') if today_record else None,
            'checkOutTime': today_record.get('checkOut') if today_record else None,
            'currentBreak': active_break,
            'breaksToday': breaks_today_count,
            'location': today_record.get('location') if today_record else None,
            'workMode': today_record.get('workMode') if today_record else None
        }
        
        return jsonify({'success': True, 'status': status_info})
        
    except Exception as e:
        print(f"Error getting status: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== ATTENDANCE SUMMARY API (MongoDB Implemented) ====================

@app.route('/api/attendance/summary', methods=['POST'])
def get_attendance_summary():
    # ... (Authentication, data fetching, and calculation logic remain the same, relying on updated helper functions)
    data = request.json
    username = session.get('username')
    
    if not username:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        start_date = data.get('startDate')
        end_date = data.get('endDate')
        department = data.get('department', 'all')
        report_type = data.get('reportType', 'daily')
        
        attendance_data = get_filtered_attendance_data(start_date, end_date, department)
        summary = calculate_attendance_summary_stats(attendance_data, start_date, end_date)
        analytics = generate_attendance_analytics(attendance_data)
        employee_stats = get_employee_statistics(attendance_data)
        
        return jsonify({
            'success': True,
            'summary': summary,
            'analytics': analytics,
            'employeeStats': employee_stats,
            'records': attendance_data,
            'filters': {
                'startDate': start_date, 'endDate': end_date, 'department': department, 'reportType': report_type
            }
        })
        
    except Exception as e:
        print(f"Error in attendance summary: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/attendance/export/csv', methods=['POST'])
def export_attendance_csv():
    # ... (Authentication, data fetching, and CSV generation logic remain the same)
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        start_date = data.get('startDate')
        end_date = data.get('endDate')
        department = data.get('department', 'all')
        
        attendance_data = get_filtered_attendance_data(start_date, end_date, department)
        
        csv_content = "Employee,Department,Date,Status,Check-in,Check-out,Hours Worked,Location,Work Mode\n"
        for record in attendance_data:
            csv_content += f"\"{record['username']}\",\"{record['department']}\",\"{record['date']}\","
            csv_content += f"\"{record.get('status', '')}\",\"{record.get('checkIn', '')}\",\"{record.get('checkOut', '')}\","
            csv_content += f"\"{record.get('hoursWorked', 0)}\",\"{record.get('location', '')}\","
            csv_content += f"\"{record.get('workMode', '')}\"\n"
        
        return jsonify({
            'success': True,
            'csvContent': csv_content,
            'filename': f"attendance_report_{start_date}_to_{end_date}.csv"
        })
        
    except Exception as e:
        print(f"Error exporting CSV: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== TEAMS & PROJECTS API (MongoDB Implemented) ====================

@app.route('/api/teams', methods=['GET'])
def get_teams():
    # Fetch all teams and return
    teams = list(teams_collection.find({}))
    return jsonify(to_json_serializable(teams))

@app.route('/api/teams', methods=['POST'])
def create_team():
    data = request.json
    username = session.get('username')
    team_name = data.get('name', '').strip()
    
    if not username or not team_name: return jsonify({'success': False}), 400
    
    new_team = {
        'id': int(datetime.now().timestamp() * 1000),
        'name': team_name,
        'members': [username],
        'projects': [],
        'chat': [],
        'meetings': [],
        'documents': []
    }
    
    result = teams_collection.insert_one(new_team)
    new_team['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'team': new_team})

@app.route('/api/teams/<int:team_id>/join', methods=['POST'])
def join_team(team_id):
    username = session.get('username')
    if not username: return jsonify({'success': False}), 401
    
    # Use $addToSet to add the member only if they don't exist
    result = teams_collection.update_one(
        {'id': team_id, 'members': {'$ne': username}},
        {'$addToSet': {'members': username}}
    )
    
    if result.matched_count > 0:
        team = teams_collection.find_one({'id': team_id})
        return jsonify({'success': True, 'team': to_json_serializable(team)})
    
    if teams_collection.find_one({'id': team_id}):
         return jsonify({'success': True, 'team': to_json_serializable(teams_collection.find_one({'id': team_id}))}) # Already a member
    
    return jsonify({'success': False}), 404

# --- Projects API (MongoDB Implemented) ---
@app.route('/api/teams/<int:team_id>/projects', methods=['POST'])
def create_project(team_id):
    data = request.json
    username = session.get('username')
    project_name = data.get('name', '').strip()
    
    if not username or not project_name: return jsonify({'success': False}), 400
    
    new_project = {
        'id': int(datetime.now().timestamp() * 1000),
        'name': project_name,
        'tasks': [],
        'createdBy': username
    }

    result = teams_collection.update_one(
        {'id': team_id},
        {'$push': {'projects': new_project}}
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'project': new_project})
    
    return jsonify({'success': False}), 404

# --- Tasks API (MongoDB Implemented) ---
@app.route('/api/teams/<int:team_id>/projects/<int:project_id>/tasks', methods=['POST'])
def add_task(team_id, project_id):
    data = request.json
    username = session.get('username')
    task_text = data.get('text', '').strip()
    
    if not username or not task_text: return jsonify({'success': False}), 400
    
    new_task = {
        'id': int(datetime.now().timestamp() * 1000),
        'text': task_text,
        'completed': False,
        'assignedTo': username,
        'status': 'todo',
        'dueDate': None
    }

    result = teams_collection.update_one(
        {'id': team_id, 'projects.id': project_id},
        {'$push': {'projects.$.tasks': new_task}}
    )

    if result.matched_count > 0:
        return jsonify({'success': True, 'task': new_task})
    
    return jsonify({'success': False}), 404

@app.route('/api/teams/<int:team_id>/projects/<int:project_id>/tasks/<int:task_id>/toggle', methods=['POST'])
def toggle_task(team_id, project_id, task_id):
    
    # Find the current state of the task
    team = teams_collection.find_one({'id': team_id, 'projects.id': project_id})
    if not team: return jsonify({'success': False}), 404
    
    # MongoDB query to find and update nested array element
    # Need to get the current completion state first
    current_state = False
    for project in team.get('projects', []):
        if project['id'] == project_id:
            for task in project.get('tasks', []):
                if task['id'] == task_id:
                    current_state = task['completed']
                    break
            break

    new_state = not current_state
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$set': {'projects.$[p].tasks.$[t].completed': new_state}},
        array_filters=[{'p.id': project_id}, {'t.id': task_id}]
    )
    
    if result.matched_count > 0:
        updated_task = teams_collection.find_one({'id': team_id}, {'_id': 0, 'projects.tasks': {'$elemMatch': {'id': task_id}}})
        # Note: Extracting the specific task from the complex MongoDB response is involved, 
        # so for simplicity, we return the expected new state.
        return jsonify({'success': True, 'task': {'id': task_id, 'completed': new_state}})
    
    return jsonify({'success': False}), 404

@app.route('/api/teams/<int:team_id>/projects/<int:project_id>/tasks/<int:task_id>', methods=['PUT'])
def update_task(team_id, project_id, task_id):
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    update_fields = {}
    if 'text' in data: update_fields['projects.$[p].tasks.$[t].text'] = data['text']
    if 'status' in data: update_fields['projects.$[p].tasks.$[t].status'] = data['status']
    if 'dueDate' in data: update_fields['projects.$[p].tasks.$[t].dueDate'] = data['dueDate']
    if 'assignedTo' in data: update_fields['projects.$[p].tasks.$[t].assignedTo'] = data['assignedTo']

    if not update_fields: return jsonify({'success': False, 'message': 'No fields to update'}), 400

    result = teams_collection.update_one(
        {'id': team_id},
        {'$set': update_fields},
        array_filters=[{'p.id': project_id}, {'t.id': task_id}]
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'task': {'id': task_id, **data}})
    
    return jsonify({'success': False}), 404

@app.route('/api/teams/<int:team_id>/projects/<int:project_id>/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(team_id, project_id, task_id):
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    # Use $pull with arrayFilters on the tasks array inside the projects array
    result = teams_collection.update_one(
        {'id': team_id},
        {'$pull': {'projects.$[p].tasks': {'id': task_id}}},
        array_filters=[{'p.id': project_id}]
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True})
    
    return jsonify({'success': False}), 404

# Reorder is complicated for MongoDB's nested array model, skipping for brevity
@app.route('/api/teams/<int:team_id>/projects/<int:project_id>/tasks/<int:task_id>/reorder', methods=['POST'])
def reorder_task(team_id, project_id, task_id):
    # This requires fetching the document, manipulating the array in Flask, and then pushing the entire array back.
    # We will return 404 to indicate not fully supported in the current MongoDB simple implementation.
    return jsonify({'success': False, 'message': 'Reordering nested arrays is complex and is not implemented in this version.'}), 404


# --- Chat API (MongoDB Implemented) ---
@app.route('/api/teams/<int:team_id>/chat', methods=['POST'])
def send_message(team_id):
    data = request.json
    username = session.get('username')
    message = data.get('message', '').strip()
    
    if not username or not message: return jsonify({'success': False}), 400
    
    new_message = {
        'id': int(datetime.now().timestamp() * 1000),
        'user': username,
        'message': message,
        'timestamp': datetime.now().strftime('%I:%M:%S %p')
    }
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$push': {'chat': new_message}}
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'message': new_message})
    
    return jsonify({'success': False}), 404

# --- Meetings API (MongoDB Implemented) ---
@app.route('/api/teams/<int:team_id>/meetings', methods=['POST'])
def schedule_meeting(team_id):
    data = request.json
    username = session.get('username')
    
    team = teams_collection.find_one({'id': team_id})
    if not team: return jsonify({'success': False}), 404
    
    new_meeting = {
        'id': int(datetime.now().timestamp() * 1000),
        'title': data.get('title'),
        'date': data.get('date'),
        'time': data.get('time'),
        'type': data.get('type'),
        'scheduledBy': username,
        'attendees': team.get('members', [])
    }
    
    system_message = {
        'id': int(datetime.now().timestamp() * 1000),
        'user': 'System',
        'message': f"{username} scheduled a meeting: {data.get('title')} on {data.get('date')} at {data.get('time')}",
        'timestamp': datetime.now().strftime('%I:%M:%S %p')
    }
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$push': {'meetings': new_meeting, 'chat': system_message}}
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'meeting': new_meeting})
    
    return jsonify({'success': False}), 404

# --- Documents API (MongoDB Implemented) ---
@app.route('/api/teams/<int:team_id>/documents', methods=['POST'])
def upload_document(team_id):
    data = request.json
    username = session.get('username')
    
    doc_name = data.get('name')
    new_doc = {
        'id': int(datetime.now().timestamp() * 1000),
        'name': doc_name,
        'size': data.get('size'),
        'type': data.get('type'),
        'uploadedBy': username,
        'uploadedAt': datetime.now().strftime('%Y-%m-%d %I:%M:%S %p'),
        'data': data.get('data')
    }
    
    system_message = {
        'id': int(datetime.now().timestamp() * 1000),
        'user': 'System',
        'message': f"{username} uploaded {doc_name}",
        'timestamp': datetime.now().strftime('%I:%M:%S %p')
    }
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$push': {'documents': new_doc, 'chat': system_message}}
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'document': new_doc})
    
    return jsonify({'success': False}), 404

@app.route('/api/teams/<int:team_id>/documents/<int:doc_id>', methods=['DELETE'])
def delete_document(team_id, doc_id):
    username = session.get('username')
    if not username: return jsonify({'success': False}), 401
    
    team = teams_collection.find_one({'id': team_id})
    if not team: return jsonify({'success': False}), 404
    
    doc_name = next((doc['name'] for doc in team.get('documents', []) if doc['id'] == doc_id), 'a document')
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$pull': {'documents': {'id': doc_id}}}
    )
    
    if result.matched_count > 0:
        system_message = {
            'id': int(datetime.now().timestamp() * 1000),
            'user': 'System',
            'message': f"{username} deleted {doc_name}",
            'timestamp': datetime.now().strftime('%I:%M:%S %p')
        }
        teams_collection.update_one({'id': team_id}, {'$push': {'chat': system_message}})
        return jsonify({'success': True})
    
    return jsonify({'success': False}), 404

# --- Recording API (MongoDB Implemented) ---
@app.route('/api/recordings', methods=['POST'])
def save_recording():
    data = request.json
    username = session.get('username')
    team_id = data.get('teamId')
    
    if not username: return jsonify({'success': False}), 401
    
    recording = {
        'id': int(datetime.now().timestamp() * 1000),
        'name': f"Meeting Recording - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        'size': len(data.get('data', '')),
        'type': 'video/webm',
        'uploadedBy': username,
        'uploadedAt': datetime.now().strftime('%Y-%m-%d %I:%M:%S %p'),
        'data': data.get('data')
    }
    
    system_message = {
        'id': int(datetime.now().timestamp() * 1000),
        'user': 'System',
        'message': f"{username} saved a meeting recording",
        'timestamp': datetime.now().strftime('%I:%M:%S %p')
    }
    
    result = teams_collection.update_one(
        {'id': team_id},
        {'$push': {'documents': recording, 'chat': system_message}}
    )
    
    if result.matched_count > 0:
        return jsonify({'success': True, 'recording': recording})

    return jsonify({'success': False}), 404

# ==================== KANBAN & COLLABORATION API (MongoDB Implemented) ====================

# --- Kanban API ---
@app.route('/api/kanban', methods=['GET'])
def get_kanban_cards():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    cards = list(kanban_collection.find({}))
    return jsonify(to_json_serializable(cards))

@app.route('/api/kanban', methods=['POST'])
def create_kanban_card():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    new_card = {
        'id': int(datetime.now().timestamp() * 1000),
        'title': data.get('title'),
        'description': data.get('description', ''),
        'priority': data.get('priority', 'medium'),
        'dueDate': data.get('dueDate'),
        'status': data.get('status', 'todo'),
        'createdBy': username,
        'createdAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    result = kanban_collection.insert_one(new_card)
    new_card['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'card': new_card})

@app.route('/api/kanban/<int:card_id>/status', methods=['PUT'])
def update_kanban_card_status(card_id):
    data = request.json
    new_status = data.get('status')
    
    result = kanban_collection.update_one(
        {'id': card_id},
        {'$set': {'status': new_status}}
    )
    
    if result.matched_count > 0:
        card = kanban_collection.find_one({'id': card_id})
        return jsonify({'success': True, 'card': to_json_serializable(card)})
    
    return jsonify({'success': False}), 404

# --- Whiteboard API ---
@app.route('/api/whiteboard/save', methods=['POST'])
def save_whiteboard():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    whiteboard_data = {
        'imageData': data.get('data'),
        'savedBy': username,
        'savedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    whiteboard_collection.update_one(
        {'id': 1}, # Use a fixed ID for the single whiteboard document
        {'$set': whiteboard_data},
        upsert=True
    )
    
    return jsonify({'success': True})

@app.route('/api/whiteboard/load', methods=['GET'])
def load_whiteboard():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    whiteboard = whiteboard_collection.find_one({'id': 1})
    return jsonify(to_json_serializable(whiteboard if whiteboard else {}))


# --- Collaborative Editor API ---
@app.route('/api/document/save', methods=['POST'])
def save_document():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    document_data = {
        'content': data.get('content'),
        'lastEditedBy': username,
        'lastEditedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    document_collection.update_one(
        {'id': 1},
        {'$set': document_data},
        upsert=True
    )
    
    return jsonify({'success': True})

@app.route('/api/document/load', methods=['GET'])
def load_document():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    document = document_collection.find_one({'id': 1})
    return jsonify(to_json_serializable(document if document else {}))

@app.route('/api/document/comments', methods=['GET'])
def get_document_comments():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    comments = list(document_comments_collection.find({}))
    return jsonify(to_json_serializable(comments))

@app.route('/api/document/comments', methods=['POST'])
def add_document_comment():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    new_comment = {
        'id': int(datetime.now().timestamp() * 1000),
        'text': data.get('text'),
        'username': username,
        'createdAt': datetime.now().isoformat()
    }
    
    result = document_comments_collection.insert_one(new_comment)
    new_comment['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'comment': new_comment})


# --- Calendar API ---
@app.route('/api/calendar/events', methods=['GET'])
def get_calendar_events():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    calendar_events = list(calendar_events_collection.find({}))
    
    # Include team meetings as calendar events
    for team in teams_collection.find({}):
        for meeting in team.get('meetings', []):
            calendar_events.append({
                'id': meeting['id'],
                'title': meeting['title'],
                'date': meeting['date'],
                'time': meeting['time'],
                'type': meeting['type'],
                'attendees': team.get('members', [])
            })
    
    return jsonify(to_json_serializable(calendar_events))

@app.route('/api/calendar/events', methods=['POST'])
def add_calendar_event():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    new_event = {
        'id': int(datetime.now().timestamp() * 1000),
        'title': data.get('title'),
        'date': data.get('date'),
        'time': data.get('time'),
        'attendees': data.get('attendees', []),
        'createdBy': username
    }
    
    result = calendar_events_collection.insert_one(new_event)
    new_event['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'event': new_event})


# --- Polling System API ---
@app.route('/api/polls', methods=['GET'])
def get_polls():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    polls = list(polls_collection.find({}))
    return jsonify(to_json_serializable(polls))

@app.route('/api/polls', methods=['POST'])
def create_poll():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    options_with_votes = [
        {'text': opt, 'votes': 0, 'voters': []}
        for opt in data.get('options', [])
    ]
    
    new_poll = {
        'id': int(datetime.now().timestamp() * 1000),
        'question': data.get('question'),
        'options': options_with_votes,
        'anonymous': data.get('anonymous', False),
        'status': 'active',
        'createdBy': username,
        'createdAt': data.get('createdAt')
    }
    
    result = polls_collection.insert_one(new_poll)
    new_poll['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'poll': new_poll})

@app.route('/api/polls/<int:poll_id>/vote', methods=['POST'])
def vote_poll(poll_id):
    data = request.json
    username = session.get('username')
    option_index = data.get('optionIndex')
    
    if not username: return jsonify({'success': False}), 401
    
    poll = polls_collection.find_one({'id': poll_id})
    if not poll: return jsonify({'success': False}), 404
    
    # Check if already voted
    if any(username in opt['voters'] for opt in poll.get('options', [])):
        return jsonify({'success': False, 'message': 'Already voted'}), 400
    
    if option_index < 0 or option_index >= len(poll['options']):
         return jsonify({'success': False, 'message': 'Invalid option index'}), 400

    # Record vote using positional operator ($)
    # Note: Requires specific query structure to update nested array elements
    update_result = polls_collection.update_one(
        {'id': poll_id},
        {
            '$inc': {f'options.{option_index}.votes': 1},
            '$push': {f'options.{option_index}.voters': username}
        }
    )
    
    if update_result.matched_count > 0:
        updated_poll = polls_collection.find_one({'id': poll_id})
        return jsonify({'success': True, 'poll': to_json_serializable(updated_poll)})

    return jsonify({'success': False}), 500

@app.route('/api/polls/<int:poll_id>/close', methods=['POST'])
def close_poll(poll_id):
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    poll = polls_collection.find_one({'id': poll_id})
    if not poll: return jsonify({'success': False}), 404
    
    if poll.get('createdBy') != username:
        return jsonify({'success': False, 'message': 'Unauthorized to close poll'}), 403
            
    result = polls_collection.update_one(
        {'id': poll_id},
        {'$set': {'status': 'closed'}}
    )
    
    if result.matched_count > 0:
        updated_poll = polls_collection.find_one({'id': poll_id})
        return jsonify({'success': True, 'poll': to_json_serializable(updated_poll)})

    return jsonify({'success': False}), 500

# --- Time Tracking API ---
@app.route('/api/time-tracking', methods=['GET'])
def get_time_entries():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    user_entries = list(time_tracking_collection.find({'username': session.get('username')}))
    return jsonify(to_json_serializable(user_entries))

@app.route('/api/time-tracking', methods=['POST'])
def add_time_entry():
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    new_entry = {
        'id': int(datetime.now().timestamp() * 1000),
        'taskName': data.get('taskName'),
        'duration': data.get('duration'),
        'date': data.get('date'),
        'username': username
    }
    
    result = time_tracking_collection.insert_one(new_entry)
    new_entry['_id'] = str(result.inserted_id)
    
    return jsonify({'success': True, 'entry': new_entry})

# --- Analytics API ---
@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    if 'username' not in session: return jsonify({'success': False}), 401
    
    # Fetch all tasks from all teams
    teams = list(teams_collection.find({}))
    all_tasks = [task for team in teams for project in team.get('projects', []) for task in project.get('tasks', [])]
    
    total_tasks = len(all_tasks)
    completed_tasks = sum(1 for task in all_tasks if task.get('completed'))
    
    completion_rate = int((completed_tasks / total_tasks * 100)) if total_tasks > 0 else 0
    
    # Generate activity data (last 28 days) - Placeholder
    activity_data = [random.randint(0, 4) for _ in range(28)]
    
    # Task statistics
    task_stats = {'todo': 0, 'inprogress': 0, 'done': 0, 'blocked': 0}
    for task in all_tasks:
        status = task.get('status', 'todo')
        if status in task_stats: task_stats[status] += 1
    
    # Productivity data (last 7 days) - Placeholder
    productivity_data = [random.randint(60, 95) for _ in range(7)]
    
    messages_sent = sum(len(t.get('chat', [])) for t in teams)

    analytics_data = {
        'completionRate': completion_rate,
        'dailyActiveUsers': len(active_users),
        'avgSessionTime': '45m',
        'messagesSent': messages_sent,
        'tasksCompleted': completed_tasks,
        'activityData': activity_data,
        'taskStats': task_stats,
        'productivityData': productivity_data
    }
    
    return jsonify(analytics_data)

# --- AI Assistant API ---
@app.route('/api/ai-summary', methods=['POST'])
def get_ai_summary():
    # Placeholder for AI logic
    data = request.json
    username = session.get('username')
    
    if not username: return jsonify({'success': False}), 401
    
    task_name = data.get('taskName')
    time_spent = data.get('timeSpent', 0)
    
    hours = time_spent // 3600
    minutes = (time_spent % 3600) // 60
    
    summary = f"""Task: {task_name}
Time Spent: {hours}h {minutes}m

Summary: You've been working on "{task_name}" for {hours} hours and {minutes} minutes. This represents good focused time on this task. 

Recommendations:
- Consider taking a short break if you've been working continuously
- Document your progress and key findings
- Update task status if significant progress was made
- Communicate with team members about your work

Productivity Insight: Sessions between 1-2 hours with short breaks optimize focus and retention."""
    
    return jsonify({'success': True, 'summary': summary})

# ==================== SOCKETIO EVENTS (Collaboration) ====================

@socketio.on('cursor-move')
def handle_cursor_move(data):
    emit('remote-cursor', {'username': data.get('username'), 'x': data.get('x'), 'y': data.get('y')}, broadcast=True, include_self=False)

@socketio.on('whiteboard-draw')
def handle_whiteboard_draw(data):
    emit('whiteboard-data', data, broadcast=True, include_self=False)

@socketio.on('whiteboard-clear')
def handle_whiteboard_clear():
    emit('whiteboard-clear', broadcast=True, include_self=False)

@socketio.on('editor-update')
def handle_editor_update(data):
    emit('editor-sync', {'content': data.get('content'), 'user': data.get('user')}, broadcast=True, include_self=False)

@socketio.on('attendance-status-request')
def handle_attendance_status_request():
    # Helper to get current status and emit it
    try:
        username = session.get('username')
        if not username: return
        
        today = datetime.now().strftime('%Y-%m-%d')
        user_id = get_user_id(username)
        
        today_record = attendance_records_collection.find_one({'userId': user_id, 'date': today})
        active_break = breaks_collection.find_one({'username': username, 'endTime': None})
        if active_break: active_break['_id'] = str(active_break['_id'])

        status_info = {
            'checkedIn': today_record is not None and today_record.get('checkOut') is None,
            'checkInTime': today_record.get('checkIn') if today_record else None,
            'currentBreak': active_break
        }
        emit('attendance-status-update', status_info)
    except Exception as e:
        print(f"Error in socket attendance status: {e}")

# --- WebRTC Signaling ---
@socketio.on('join-room')
def on_join_call(data):
    username = session.get('username')
    room = f"team_call_{data['teamId']}"
    join_room(room)
    emit('user-joined', {'username': username, 'sid': request.sid}, to=room, include_self=False)

@socketio.on('offer')
def handle_offer(data):
    emit('offer', {'from_sid': request.sid, 'offer': data.get('offer')}, to=data.get('to_sid'))

@socketio.on('answer')
def handle_answer(data):
    emit('answer', {'from_sid': request.sid, 'answer': data.get('answer')}, to=data.get('to_sid'))

@socketio.on('ice-candidate')
def handle_ice_candidate(data):
    emit('ice-candidate', {'from_sid': request.sid, 'candidate': data.get('candidate')}, to=data.get('to_sid'))
    
@socketio.on('leave-room')
def on_leave_call(data):
    room = f"team_call_{data['teamId']}"
    leave_room(room)
    emit('user-left', {'sid': request.sid}, to=room, include_self=False)

# --- Main Run Block ---
if __name__ == '__main__':
    # Using eventlet as specified by original code's async_mode
    socketio.run(app, debug=True, port=5000)
