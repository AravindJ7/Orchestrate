from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash
import os
import sys
import json
import csv
import tempfile
import shutil
from pathlib import Path
from werkzeug.utils import secure_filename

# Add parent directories to path to import from code/
ROOT_DIR = Path(__file__).resolve().parent.parent
CODE_DIR = ROOT_DIR / 'hackerrank-orchestrate-october26' / 'code'
DATASET_DIR = ROOT_DIR / 'hackerrank-orchestrate-october26' / 'dataset'
OUTPUT_DIR = ROOT_DIR

sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(ROOT_DIR))

from data_index import DataIndex
from evidence_parser import EvidenceParser
from audit_engine import AuditEngine, DeletionAuditAgent, is_deletion_authorized

app = Flask(__name__)
app.secret_key = 'ghost-data-still-exists-ui'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max
app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()

ALLOWED_EXTENSIONS = {'csv', 'json', 'sql', 'txt', 'log', 'pdf', 'png', 'jpg', 'jpeg'}

# Global instances (lazy loaded)
data_index = None
evidence_parser = None
audit_agent = None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def init_agent():
    global data_index, evidence_parser, audit_agent
    if data_index is None:
        data_index = DataIndex(DATASET_DIR)
        evidence_parser = EvidenceParser(data_index)
        evidence_parser.parse_all()
        audit_agent = DeletionAuditAgent(dataset_dir=DATASET_DIR, output_dir=OUTPUT_DIR)
    return audit_agent

@app.route('/')
def index():
    init_agent()
    return render_template('index.html', 
                         total_cases=len(data_index.deletion_requests) if data_index else 518)

@app.route('/full_dataset')
def full_dataset():
    init_agent()
    return render_template('full_dataset.html')

@app.route('/run_full_dataset', methods=['POST'])
def run_full_dataset():
    try:
        agent = init_agent()
        rows = agent.run()
        return jsonify({
            'success': True,
            'message': f'Generated output.csv with {len(rows)} rows',
            'count': len(rows),
            'output_path': str(OUTPUT_DIR / 'output.csv')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download_output')
def download_output():
    output_path = OUTPUT_DIR / 'output.csv'
    if output_path.exists():
        return send_file(output_path, as_attachment=True, download_name='output.csv')
    else:
        flash('output.csv not found. Run the full dataset analysis first.', 'error')
        return redirect(url_for('full_dataset'))

@app.route('/single_case')
def single_case():
    init_agent()
    return render_template('single_case.html')

@app.route('/analyze_single', methods=['POST'])
def analyze_single():
    try:
        init_agent()
        case = {
            'case_id': request.form.get('case_id') or 'custom_case',
            'email': request.form.get('email', ''),
            'requested_at': request.form.get('requested_at', ''),
            'reported_status': request.form.get('reported_status', 'UNKNOWN'),
            'reported_at': request.form.get('reported_at', ''),
            'audit_at': request.form.get('audit_at', ''),
            'request_text': request.form.get('request_text', '')
        }
        
        status, steps = audit_agent.engine.analyze_case(case)
        steps_json = json.dumps(steps, separators=(',', ':'))
        
        result = {
            'case_id': case['case_id'],
            'deletion_status': status,
            'steps_json': steps_json,
            'steps': steps
        }
        
        return render_template('result.html', result=result, case=case)
    except Exception as e:
        flash(f'Error analyzing case: {str(e)}', 'error')
        return redirect(url_for('single_case'))

@app.route('/upload_analyze')
def upload_analyze():
    init_agent()
    return render_template('upload_analyze.html')

@app.route('/analyze_upload', methods=['POST'])
def analyze_upload():
    try:
        init_agent()
        
        # Get form data
        case = {
            'case_id': request.form.get('case_id') or 'uploaded_case',
            'email': request.form.get('email', ''),
            'requested_at': request.form.get('requested_at', ''),
            'reported_status': request.form.get('reported_status', 'UNKNOWN'),
            'reported_at': request.form.get('reported_at', ''),
            'audit_at': request.form.get('audit_at', ''),
            'request_text': request.form.get('request_text', '')
        }
        
        # Handle file uploads
        uploaded_files = []
        if 'evidence_files' in request.files:
            files = request.files.getlist('evidence_files')
            for file in files:
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(filepath)
                    uploaded_files.append(filepath)
        
        # If files uploaded, we need to parse them
        # For now, use the existing agent which has parsed the dataset
        # In a full implementation, we'd create a temporary parser for uploaded files
        status, steps = audit_agent.engine.analyze_case(case)
        steps_json = json.dumps(steps, separators=(',', ':'))
        
        result = {
            'case_id': case['case_id'],
            'deletion_status': status,
            'steps_json': steps_json,
            'steps': steps,
            'uploaded_files': len(uploaded_files)
        }
        
        return render_template('result.html', result=result, case=case)
    except Exception as e:
        flash(f'Error analyzing uploaded files: {str(e)}', 'error')
        return redirect(url_for('upload_analyze'))

@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    try:
        init_agent()
        data = request.get_json()
        case = {
            'case_id': data.get('case_id') or 'api_case',
            'email': data.get('email', ''),
            'requested_at': data.get('requested_at', ''),
            'reported_status': data.get('reported_status', 'UNKNOWN'),
            'reported_at': data.get('reported_at', ''),
            'audit_at': data.get('audit_at', ''),
            'request_text': data.get('request_text', '')
        }
        status, steps = audit_agent.engine.analyze_case(case)
        return jsonify({
            'case_id': case['case_id'],
            'deletion_status': status,
            'steps_json': json.dumps(steps, separators=(',', ':')),
            'steps': steps
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_agent()
    app.run(debug=True, host='0.0.0.0', port=5000)
