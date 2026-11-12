#!/usr/bin/env python3
"""
Simple web interface for Estimate Review prototype.
"""

from flask import Flask, request, render_template_string, jsonify
import os
import tempfile
from estimate_analyzer import analyze_estimate

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Estimate Review - Ben's Body Works</title>
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; max-width: 1400px; margin: 0 auto; background: #f8fafc; color: #1e293b; }
        .topbar { background: white; border-bottom: 1px solid #e2e8f0; padding: 12px 24px; display: flex; align-items: center; gap: 24px; }
        .logo { font-weight: 700; font-size: 1.3em; color: #1e40af; }
        .container { padding: 20px 24px; }
        .header { margin-bottom: 16px; }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
        .stat { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; }
        .stat .label { font-size: 0.7em; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
        .stat .value { font-size: 1.5em; font-weight: 700; color: #0f172a; margin-top: 2px; }
        .stat .sub { font-size: 0.8em; color: #64748b; }
        .category { background: white; border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 6px; }
        .cat-header { padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; cursor: pointer; }
        .cat-header:hover { background: #f8fafc; }
        .cat-name { font-weight: 600; color: #1e40af; }
        .cat-count { color: #64748b; font-size: 0.85em; margin-left: 8px; }
        .cat-amount { font-weight: 600; color: #0369a1; }
        .ops { display: none; padding: 0 16px 12px; }
        .op { border-left: 3px solid #0284c7; padding: 10px 12px; margin: 8px 0; background: #f8fafc; border-radius: 4px; }
        .op-header { display: flex; justify-content: space-between; }
        .op-title { font-weight: 500; }
        .op-amount { font-weight: 600; color: #0369a1; }
        .why { background: #f1f5f9; padding: 6px 10px; border-radius: 4px; margin-top: 6px; font-size: 0.9em; }
        .trigger { background: #f0f9ff; padding: 6px 10px; border-radius: 4px; font-size: 0.9em; color: #0369a1; }
        .upload { border: 2px dashed #94a3b8; padding: 50px; text-align: center; border-radius: 12px; background: white; margin: 40px; }
        button { background: #1e40af; color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-weight: 500; }
        button:hover { background: #1e3a8a; }
        .btn-secondary { background: #059669; }
        .badge { background: #dbeafe; color: #1e40af; padding: 2px 8px; border-radius: 10px; font-size: 0.75em; margin-left: 8px; }
    </style>
    <script>
        function toggle(id) {
            const el = document.getElementById(id);
            el.style.display = el.style.display === 'block' ? 'none' : 'block';
        }
    </script>
</head>
<body>
    <div class="topbar">
        <div class="logo">Estimate Review</div>
        <div>Ben's Body Works</div>
    </div>
    <div class="container">
    {% if not result %}
    <div class="upload">
        <form method="post" enctype="multipart/form-data">
            <h3>Drop CCC ONE estimate PDF</h3>
            <input type="file" name="pdf" accept=".pdf"><br><br>
            <label>Optional: Vehicle Equipment Report (build sheet)</label><br>
            <input type="file" name="build_sheet" accept=".pdf"><br><br>
            <button type="submit">Analyze Estimate</button>
            <button type="submit" name="demo" value="1" class="btn-secondary" style="margin-left:12px;">Demo with Sample</button>
        </form>
    </div>
    {% else %}
    <div class="header">
        <h1>{{ result.estimate.year_make_model or 'Vehicle' }}</h1>
        <p>RO #{{ result.estimate.ro_number or 'N/A' }}</p>
    </div>
    <div class="stats">
        <div class="stat">
            <div class="label">Missed Revenue</div>
            <div class="value">${{ "%.2f"|format(result.total_potential) }}</div>
            <div class="sub">{{ result.suggestion_count }} operations</div>
        </div>
        <div class="stat">
            <div class="label">Selected</div>
            <div class="value">$0.00</div>
            <div class="sub">0 of {{ result.suggestion_count }}</div>
        </div>
        <div class="stat">
            <div class="label">Base Estimate</div>
            <div class="value">${{ "%.2f"|format(result.estimate.total or 0) }}</div>
            <div class="sub">Estimate grand total</div>
        </div>
        <div class="stat">
            <div class="label">Needs Input</div>
            <div class="value">0</div>
            <div class="sub">cards to finish</div>
        </div>
    </div>

    <h3 style="margin-bottom:8px;">MISSED OPS <span class="badge">{{ result.suggestion_count }}</span></h3>
    {% set categories = {} %}
    {% for s in result.suggestions %}
        {% if s.category not in categories %}
            {% set _ = categories.update({s.category: []}) %}
        {% endif %}
        {% set _ = categories[s.category].append(s) %}
    {% endfor %}
    {% for cat, items in categories.items() %}
    <div class="category">
        <div class="cat-header" onclick="toggle('cat-{{ loop.index }}')">
            <div>
                <span class="cat-name">{{ cat }}</span>
                <span class="cat-count">{{ items|length }} operations</span>
            </div>
            <div class="cat-amount">${{ "%.2f"|format(items|sum(attribute='amount')) }}</div>
        </div>
        <div class="ops" id="cat-{{ loop.index }}">
            {% for s in items %}
            <div class="op">
                <div class="op-header">
                    <div class="op-title">{{ s.description }}</div>
                    <div class="op-amount">${{ "%.2f"|format(s.amount) }}</div>
                </div>
                {% if s.trigger %}
                <div class="section">
                    <div class="section-label">Trigger</div>
                    <div class="trigger">{{ s.trigger }}</div>
                </div>
                {% endif %}
                <div class="section">
                    <div class="section-label">Recommendation</div>
                    <div>{{ s.description }}</div>
                </div>
                <div class="section">
                    <div class="section-label">Why</div>
                    <div class="why">{{ s.why or 'Required based on repair operations present on the estimate.' }}</div>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endfor %}
    {% endif %}
    </div>
</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def index():
    result = None
    if request.method == 'POST':
        if 'demo' in request.form:
            # Load a sample estimate for demo
            sample = '/workspace/attachments/3722.pdf'
            if os.path.exists(sample):
                result = analyze_estimate(sample)
        elif 'pdf' in request.files:
            f = request.files['pdf']
            if f.filename == '':
                return "No file selected", 400
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                f.save(tmp.name)
                build_sheet_tmp = None
                if 'build_sheet' in request.files and request.files['build_sheet'].filename:
                    bs = request.files['build_sheet']
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as bstmp:
                        bs.save(bstmp.name)
                        build_sheet_tmp = bstmp.name
                try:
                    result = analyze_estimate(tmp.name, build_sheet_tmp)
                finally:
                    os.unlink(tmp.name)
                    if build_sheet_tmp and os.path.exists(build_sheet_tmp):
                        os.unlink(build_sheet_tmp)
    
    return render_template_string(HTML, result=result)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
