import os
import subprocess
import tarfile
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

class BackupManager:
    def __init__(self):
        self.backup_dir = "/app/backups"
        os.makedirs(self.backup_dir, exist_ok=True)
        
    def backup_database(self):
        """Backup PostgreSQL database"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"{self.backup_dir}/database_{timestamp}.sql"
        
        try:
            # Dump PostgreSQL database
            subprocess.run([
                "pg_dump",
                "-h", "postgres",
                "-U", "admin",
                "-d", "cloudplatform",
                "-f", backup_file
            ], check=True, env={**os.environ, "PGPASSWORD": "password"})
            
            print(f"✅ Database backup created: {backup_file}")
            return backup_file
        except subprocess.CalledProcessError as e:
            print(f"❌ Database backup failed: {e}")
            return None
    
    def backup_app_data(self):
        """Backup application data and configurations"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"{self.backup_dir}/app_data_{timestamp}.tar.gz"
        
        try:
            with tarfile.open(backup_file, "w:gz") as tar:
                # Backup important directories
                directories_to_backup = [
                    "/app/data",
                    "/tmp/builds",
                    "/app/dashboard"
                ]
                
                for directory in directories_to_backup:
                    if os.path.exists(directory):
                        tar.add(directory, arcname=os.path.basename(directory))
            
            print(f"✅ App data backup created: {backup_file}")
            return backup_file
        except Exception as e:
            print(f"❌ App data backup failed: {e}")
            return None
    
    def upload_to_s3(self, file_path, bucket_name):
        """Upload backup to S3-compatible storage"""
        try:
            s3_client = boto3.client(
                's3',
                endpoint_url=os.getenv('S3_ENDPOINT'),
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
            )
            
            s3_client.upload_file(file_path, bucket_name, os.path.basename(file_path))
            print(f"✅ Backup uploaded to S3: {os.path.basename(file_path)}")
            return True
        except ClientError as e:
            print(f"❌ S3 upload failed: {e}")
            return False
    
    def cleanup_old_backups(self, keep_last_n=10):
        """Clean up old backup files, keep only the last N"""
        backup_files = []
        for filename in os.listdir(self.backup_dir):
            if filename.endswith(('.sql', '.tar.gz')):
                filepath = os.path.join(self.backup_dir, filename)
                backup_files.append((filepath, os.path.getctime(filepath)))
        
        # Sort by creation time (oldest first)
        backup_files.sort(key=lambda x: x[1])
        
        # Remove old backups
        for filepath, _ in backup_files[:-keep_last_n]:
            os.remove(filepath)
            print(f"🧹 Removed old backup: {filepath}")

def perform_full_backup():
    """Perform complete backup routine"""
    manager = BackupManager()
    
    print("🔄 Starting backup process...")
    
    # Create backups
    db_backup = manager.backup_database()
    data_backup = manager.backup_app_data()
    
    # Upload to cloud storage if configured
    if os.getenv('AWS_ACCESS_KEY_ID'):
        if db_backup:
            manager.upload_to_s3(db_backup, "mini-cloud-backups")
        if data_backup:
            manager.upload_to_s3(data_backup, "mini-cloud-backups")
    
    # Cleanup old backups
    manager.cleanup_old_backups()
    
    print("✅ Backup process completed")

if __name__ == "__main__":
    perform_full_backup()