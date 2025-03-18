from flask import Flask, jsonify, request, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import requests
import uuid
import base64
import json
from werkzeug.utils import secure_filename
import logging
from sqlalchemy.exc import SQLAlchemyError
import mimetypes

# Configurar logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")  
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///conteudos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Vercel Blob config
BLOB_TOKEN = os.environ.get('BLOB_READ_WRITE_TOKEN')
logger.info(f"BLOB_TOKEN configurado: {'Sim' if BLOB_TOKEN else 'Não'}")

db = SQLAlchemy(app)

class Conteudo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(100), nullable=False)
    funcionario = db.Column(db.String(100), nullable=False)
    area = db.Column(db.String(50), nullable=False, default="tecnica")
    descricao = db.Column(db.Text, nullable=True)
    arquivo = db.Column(db.String(200), nullable=True)  # Renomeado de imagem para arquivo
    tipo_arquivo = db.Column(db.String(50), nullable=True)  # Novo campo para o tipo de arquivo

# Criar tabelas se não existirem
with app.app_context():
    try:
        # Verificar se a tabela já existe
        inspector = db.inspect(db.engine)
        if not inspector.has_table(Conteudo.__tablename__):
            db.create_all()
            logger.info("Tabela criada com sucesso")
        else:
            # Verificar se as colunas existem
            columns = [column['name'] for column in inspector.get_columns(Conteudo.__tablename__)]
            with db.engine.connect() as conn:
                if 'area' not in columns:
                    conn.execute(db.text("ALTER TABLE conteudo ADD COLUMN area VARCHAR(50) DEFAULT 'tecnica' NOT NULL"))
                if 'descricao' not in columns:
                    conn.execute(db.text("ALTER TABLE conteudo ADD COLUMN descricao TEXT"))
                if 'tipo_arquivo' not in columns:
                    conn.execute(db.text("ALTER TABLE conteudo ADD COLUMN tipo_arquivo VARCHAR(50)"))
                # Renomear coluna imagem para arquivo se necessário
                if 'imagem' in columns and 'arquivo' not in columns:
                    conn.execute(db.text("ALTER TABLE conteudo RENAME COLUMN imagem TO arquivo"))
            logger.info("Colunas atualizadas com sucesso")
    except Exception as e:
        logger.error(f"Erro ao configurar banco de dados: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/conteudos', methods=['GET'])
def obter_conteudos():
    try:
        conteudos = Conteudo.query.all()
        return jsonify([{
            'id': c.id, 
            'titulo': c.titulo, 
            'funcionario': c.funcionario, 
            'area': c.area if hasattr(c, 'area') else 'tecnica',
            'descricao': c.descricao if hasattr(c, 'descricao') else '',
            'arquivo': c.arquivo,
            'tipo_arquivo': c.tipo_arquivo if hasattr(c, 'tipo_arquivo') else None
        } for c in conteudos])
    except Exception as e:
        logger.error(f"Erro ao obter conteúdos: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/conteudos', methods=['POST'])
def adicionar_conteudo():
    try:
        # Verificar se o request é JSON
        if not request.is_json:
            logger.error("Request não é JSON")
            return jsonify({'error': 'O request deve ser JSON'}), 400
            
        dados = request.get_json()
        logger.info(f"Dados recebidos: {dados}")
        
        # Validar dados obrigatórios
        if not dados.get('titulo') or not dados.get('funcionario'):
            return jsonify({'error': 'Título e funcionário são obrigatórios'}), 400
            
        # Criar novo conteúdo
        novo_conteudo = Conteudo(
            titulo=dados['titulo'], 
            funcionario=dados['funcionario'], 
            area=dados.get('area', 'tecnica'),
            descricao=dados.get('descricao', ''),
            arquivo=dados.get('arquivo'),
            tipo_arquivo=dados.get('tipo_arquivo')
        )
        
        db.session.add(novo_conteudo)
        db.session.commit()
        
        return jsonify({
            'id': novo_conteudo.id, 
            'titulo': novo_conteudo.titulo, 
            'funcionario': novo_conteudo.funcionario, 
            'area': novo_conteudo.area,
            'descricao': novo_conteudo.descricao,
            'arquivo': novo_conteudo.arquivo,
            'tipo_arquivo': novo_conteudo.tipo_arquivo
        }), 201
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Erro de banco de dados: {e}")
        return jsonify({'error': f'Erro de banco de dados: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Erro ao adicionar conteúdo: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/conteudos/<int:id>', methods=['DELETE'])
def excluir_conteudo(id):
    try:
        conteudo = Conteudo.query.get_or_404(id)
        
        # Se tiver arquivo, tentar excluir do Vercel Blob
        if conteudo.arquivo and BLOB_TOKEN and 'blob.vercel-storage.com' in conteudo.arquivo:
            try:
                # Extrair o pathname da URL
                from urllib.parse import urlparse
                path = urlparse(conteudo.arquivo).path
                blob_id = path.split('/')[-1]
                
                # Fazer requisição para deletar o blob
                headers = {'Authorization': f'Bearer {BLOB_TOKEN}'}
                delete_url = f'https://blob.vercel-storage.com/{blob_id}'
                response = requests.delete(delete_url, headers=headers)
                logger.info(f"Resposta da exclusão do blob: {response.status_code}")
            except Exception as e:
                logger.error(f"Erro ao excluir arquivo do Blob: {e}")
        
        db.session.delete(conteudo)
        db.session.commit()
        return jsonify({'message': 'Conteúdo excluído com sucesso!'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao excluir conteúdo: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_arquivo():
    logger.info("Iniciando upload de arquivo")
    
    if 'arquivo' not in request.files:
        logger.error("Nenhum arquivo enviado no request")
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400
    
    arquivo = request.files['arquivo']
    
    if arquivo.filename == '':
        logger.error("Nome do arquivo está vazio")
        return jsonify({'error': 'Nenhum arquivo selecionado'}), 400
    
    # Obter o tipo MIME do arquivo
    tipo_arquivo = arquivo.content_type
    if not tipo_arquivo or tipo_arquivo == 'application/octet-stream':
        # Tentar adivinhar o tipo pelo nome do arquivo
        tipo_arquivo = mimetypes.guess_type(arquivo.filename)[0] or 'application/octet-stream'
    
    logger.info(f"Tipo de arquivo: {tipo_arquivo}")
    
    # Verificar tamanho do arquivo (máximo 16MB)
    arquivo.seek(0, os.SEEK_END)
    tamanho = arquivo.tell()
    arquivo.seek(0)  # Voltar ao início do arquivo
    
    if tamanho > app.config['MAX_CONTENT_LENGTH']:
        logger.error(f"Arquivo muito grande: {tamanho} bytes")
        return jsonify({'error': f'Arquivo muito grande. O tamanho máximo é {app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024)}MB.'}), 400
    
    if not BLOB_TOKEN:
        logger.error("BLOB_TOKEN não configurado")
        # Se não tiver token do Blob, salvar localmente
        try:
            # Criar pasta para uploads se não existir
            upload_folder = os.path.join(app.root_path, 'static', 'uploads')
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            
            # Salvar arquivo com nome único
            filename = secure_filename(arquivo.filename)
            unique_filename = f"{uuid.uuid4()}_{filename}"
            file_path = os.path.join(upload_folder, unique_filename)
            arquivo.save(file_path)
            
            # URL relativa para o arquivo
            url = f"/static/uploads/{unique_filename}"
            logger.info(f"Arquivo salvo localmente: {url}")
            
            return jsonify({'url': url, 'tipo_arquivo': tipo_arquivo})
        except Exception as e:
            logger.error(f"Erro ao salvar arquivo localmente: {e}")
            return jsonify({'error': str(e)}), 500
    
    try:
        # Ler o conteúdo do arquivo
        file_content = arquivo.read()
        file_size = len(file_content)
        logger.info(f"Tamanho do arquivo: {file_size} bytes")
        
        # Gerar nome único para o arquivo
        filename = secure_filename(arquivo.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        
        # Preparar para upload para o Vercel Blob
        headers = {
            'Authorization': f'Bearer {BLOB_TOKEN}',
            'Content-Type': 'application/json',
        }
        
        # Codificar o conteúdo do arquivo em base64
        encoded_content = base64.b64encode(file_content).decode('utf-8')
        
        # Preparar o payload
        payload = {
            'filename': unique_filename,
            'contentType': tipo_arquivo,
            'body': encoded_content,
        }
        
        # Fazer upload para o Vercel Blob
        logger.info("Enviando requisição para Vercel Blob")
        response = requests.post(
            'https://blob.vercel-storage.com',
            headers=headers,
            data=json.dumps(payload)
        )
        
        logger.info(f"Resposta do Vercel Blob: {response.status_code}")
        
        if response.status_code != 201:
            logger.error(f"Erro na resposta do Vercel Blob: {response.text}")
            return jsonify({'error': f'Falha ao fazer upload do arquivo: {response.text}'}), 500
        
        # Obter a URL do arquivo
        blob_data = response.json()
        url = blob_data.get('url')
        logger.info(f"URL do arquivo: {url}")
        
        return jsonify({'url': url, 'tipo_arquivo': tipo_arquivo})
    
    except Exception as e:
        logger.error(f"Erro no upload: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)