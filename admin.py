from flask import Blueprint, render_template, request, redirect, url_for
from flask_login import login_required, current_user
from models import db, User
import boto3

admin_bp = Blueprint('admin_bp', __name__, template_folder='templates')

@admin_bp.route('/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        return "Access denied", 403

    users = User.query.all()

    # ✅ Fetch recipe files from S3
    # s3 = boto3.client('s3')
    # bucket_name = 'your-recipe-bucket-name'  # 🔁 Replace with your actual bucket name
    # prefix = 'recipes/'  # 🔁 Adjust if needed

    # try:
    #     response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
    #     recipe_files = [
    #         obj['Key'] for obj in response.get('Contents', [])
    #         if obj['Key'].endswith('.md')
    #     ]
    # except Exception as e:
    #     recipe_files = []
    #     print(f"Error fetching recipes from S3: {e}")

    return render_template(
        'admin_dashboard.html',  # ✅ matches your templates folder
        users=users,
        # recipes=recipe_files,
        username=current_user.username
    )

# @admin_bp.route('/update-role/<int:user_id>', methods=['POST'])
# @login_required
# def update_user_role(user_id):
#     if current_user.role != 'admin':
#         return "Access denied", 403

#     new_role = request.form.get('role')
#     user = User.query.get(user_id)
#     if user:
#         user.role = new_role
#         db.session.commit()
#     return redirect(url_for('admin_bp.admin_dashboard'))  # ✅ matches renamed function