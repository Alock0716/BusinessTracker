from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from flask import Flask, render_template, request, redirect, url_for, flash, session
from sqlalchemy import func, text
from config import Config
from models import db, Category, Platform, PaymentMethod, Customer, Product, ProductField, ProductAddon, ProductBundle, Sale, SaleItem, SaleItemField, SaleItemAddon

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Single-user authentication. Credentials come from environment variables so
# the deployed username/password never need to be committed to GitHub.
LOGIN_USERNAME = app.config.get('LOGIN_USERNAME', 'admin')
LOGIN_PASSWORD = app.config.get('LOGIN_PASSWORD', 'change-me')

@app.before_request
def require_login():
    if request.endpoint in {'login', 'static'}:
        return
    if not session.get('authenticated'):
        return redirect(url_for('login', next=request.full_path))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('authenticated'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
            session.clear()
            session['authenticated'] = True
            next_url = request.form.get('next', '')
            if not next_url or not next_url.startswith('/') or next_url.startswith('//'):
                next_url = url_for('dashboard')
            return redirect(next_url)
        flash('Invalid username or password.', 'error')
    return render_template('login.html', next=request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

with app.app_context():
    db.create_all()
    # Small migrations for databases created by earlier SQLite versions.
    # PostgreSQL databases are expected to be created from the current models.
    if db.engine.dialect.name == 'sqlite':
        try:
            cols = {r[1] for r in db.session.execute(text('PRAGMA table_info(product_addon)')).fetchall()}
            if 'description' not in cols:
                db.session.execute(text('ALTER TABLE product_addon ADD COLUMN description TEXT'))
            if 'labor_hours' not in cols:
                db.session.execute(text('ALTER TABLE product_addon ADD COLUMN labor_hours NUMERIC(12,2) DEFAULT 0'))
            sale_cols = {r[1] for r in db.session.execute(text('PRAGMA table_info(sale_item)')).fetchall()}
            if 'bundle_id' not in sale_cols:
                db.session.execute(text('ALTER TABLE sale_item ADD COLUMN bundle_id INTEGER'))
            db.session.commit()
        except Exception:
            db.session.rollback()
    if not Category.query.first():
        db.session.add_all([Category(name='General'), Category(name='Services'), Category(name='Products')])
    if not Platform.query.first():
        db.session.add_all([Platform(name='Website'), Platform(name='Etsy'), Platform(name='In Person'), Platform(name='Other')])
    if not PaymentMethod.query.first():
        db.session.add_all([PaymentMethod(name='Cash'), PaymentMethod(name='Card'), PaymentMethod(name='Gift Card'), PaymentMethod(name='Product Exchange')])
    db.session.commit()

def money(v):
    try: return Decimal(str(v or 0))
    except (InvalidOperation, ValueError, TypeError): return Decimal('0')

@app.context_processor
def helpers(): return {'money': lambda x: f'${money(x):,.2f}'}

@app.route('/')
def dashboard():
    now = datetime.utcnow(); month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue = db.session.query(func.coalesce(func.sum(Sale.total_amount),0)).scalar() or 0
    monthly = db.session.query(func.coalesce(func.sum(Sale.total_amount),0)).filter(Sale.sale_date >= month_start).scalar() or 0
    orders = Sale.query.count(); monthly_orders = Sale.query.filter(Sale.sale_date >= month_start).count()
    hours = db.session.query(func.coalesce(func.sum(SaleItem.labor_hours * SaleItem.quantity),0)).scalar() or 0
    top = db.session.query(Product.name, func.sum(SaleItem.quantity).label('qty'), func.sum(SaleItem.quantity*SaleItem.unit_price).label('rev')).join(SaleItem).group_by(Product.id).order_by(func.sum(SaleItem.quantity).desc()).limit(5).all()
    return render_template('dashboard.html', revenue=revenue, monthly=monthly, orders=orders, monthly_orders=monthly_orders, hours=hours, top=top)

@app.route('/products')
def products(): return render_template('products/list.html', products=Product.query.order_by(Product.name).all())

@app.route('/products/new', methods=['GET','POST'])
def product_new():
    if request.method == 'POST':
        p=Product(name=request.form['name'], description=request.form.get('description'), base_price=money(request.form.get('base_price')), category_id=request.form.get('category_id') or None)
        db.session.add(p); db.session.flush(); _save_product_children(p)
        db.session.commit(); flash('Product created.','success'); return redirect(url_for('products'))
    return render_template('products/form.html', product=None, categories=Category.query.order_by(Category.name).all())

@app.route('/products/<int:id>/edit', methods=['GET','POST'])
def product_edit(id):
    p=Product.query.get_or_404(id)
    if request.method=='POST':
        p.name=request.form['name']; p.description=request.form.get('description'); p.base_price=money(request.form.get('base_price')); p.category_id=request.form.get('category_id') or None
        ProductField.query.filter_by(product_id=p.id).delete(); ProductAddon.query.filter_by(product_id=p.id).delete(); ProductBundle.query.filter_by(product_id=p.id).delete()
        _save_product_children(p); db.session.commit(); flash('Product updated.','success'); return redirect(url_for('products'))
    return render_template('products/form.html', product=p, categories=Category.query.order_by(Category.name).all())

@app.route('/products/<int:id>/delete', methods=['POST'])
def product_delete(id):
    p=Product.query.get_or_404(id)
    if SaleItem.query.filter_by(product_id=p.id).first():
        flash('This product cannot be deleted because it has sales history. Deactivate it instead.','error')
    else:
        db.session.delete(p); db.session.commit(); flash('Product deleted.','success')
    return redirect(url_for('products'))

def _save_product_children(p):
    names=request.form.getlist('field_name'); types=request.form.getlist('field_type'); units=request.form.getlist('field_unit'); required=request.form.getlist('field_required')
    for i,n in enumerate(names):
        if n.strip(): db.session.add(ProductField(product=p,name=n.strip(),field_type=types[i] if i<len(types) else 'text',unit=units[i] if i<len(units) else None,required=(str(i) in required)))
    an=request.form.getlist('addon_name'); ad=request.form.getlist('addon_description'); ap=request.form.getlist('addon_price'); ah=request.form.getlist('addon_labor')
    for i,n in enumerate(an):
        if n.strip(): db.session.add(ProductAddon(product=p,name=n.strip(),description=ad[i] if i<len(ad) else None,price=money(ap[i] if i<len(ap) else 0),labor_hours=money(ah[i] if i<len(ah) else 0)))
    bn=request.form.getlist('bundle_name'); bd=request.form.getlist('bundle_description'); ba=request.form.getlist('bundle_amount'); bu=request.form.getlist('bundle_unit'); bp=request.form.getlist('bundle_price'); bh=request.form.getlist('bundle_labor')
    for i,n in enumerate(bn):
        if n.strip(): db.session.add(ProductBundle(product=p,name=n.strip(),description=bd[i] if i<len(bd) else None,amount=money(ba[i] if i<len(ba) else 1),unit=bu[i] if i<len(bu) else None,price=money(bp[i] if i<len(bp) else 0),labor_hours=money(bh[i] if i<len(bh) else 0)) )

def _product_json(products):
    return {str(p.id): {
        'price': str(p.base_price or 0),
        'fields': [{'id': f.id, 'name': f.name, 'type': f.field_type, 'unit': f.unit or '', 'required': bool(f.required)} for f in p.fields],
        'addons': [{'id': a.id, 'name': a.name, 'description': a.description or '', 'price': str(a.price or 0), 'labor': str(a.labor_hours or 0)} for a in p.addons if a.active],
        'bundles': [{'id': b.id, 'name': b.name, 'description': b.description or '', 'amount': str(b.amount or 1), 'unit': b.unit or '', 'price': str(b.price or 0), 'labor': str(b.labor_hours or 0)} for b in p.bundles if b.active]
    } for p in products}

@app.route('/sales')
def sales(): return render_template('sales/list.html', sales=Sale.query.order_by(Sale.sale_date.desc()).all())

@app.route('/sales/new', methods=['GET','POST'])
def sale_new(): return _sale_form(None)

@app.route('/sales/<int:id>/edit', methods=['GET','POST'])
def sale_edit(id): return _sale_form(Sale.query.get_or_404(id))

def _sale_form(sale):
    products=Product.query.filter_by(active=True).order_by(Product.name).all()
    if request.method=='POST':
        if sale is None:
            sale=Sale(); db.session.add(sale)
        sale.sale_date=datetime.fromisoformat(request.form['sale_date']) if request.form.get('sale_date') else datetime.utcnow()
        name=request.form.get('customer_name','').strip()
        if name:
            customer=Customer.query.filter(func.lower(Customer.name)==name.lower()).first()
            if not customer: customer=Customer(name=name); db.session.add(customer); db.session.flush()
            sale.customer=customer
        else: sale.customer_id=None
        sale.platform_id=request.form.get('platform_id') or None; sale.payment_method_id=request.form.get('payment_method_id') or None; sale.payment_detail=request.form.get('payment_detail'); sale.notes=request.form.get('notes')
        if sale.id: SaleItem.query.filter_by(sale_id=sale.id).delete()
        db.session.flush(); total=Decimal('0')
        pids=request.form.getlist('product_id'); bids=request.form.getlist('bundle_id'); qtys=request.form.getlist('quantity'); prices=request.form.getlist('unit_price'); hours=request.form.getlist('labor_hours')
        for i,pid in enumerate(pids):
            if not pid: continue
            q=money(qtys[i] if i<len(qtys) else 1); price=money(prices[i] if i<len(prices) else 0); h=money(hours[i] if i<len(hours) else 0)
            item=SaleItem(sale=sale, product_id=int(pid), bundle_id=int(bids[i]) if i<len(bids) and bids[i] else None, quantity=q, unit_price=price, labor_hours=h); db.session.add(item); db.session.flush()
            field_ids=request.form.getlist(f'field_id_{i}'); field_values=request.form.getlist(f'field_value_{i}')
            for j,fid in enumerate(field_ids):
                if fid: db.session.add(SaleItemField(sale_item=item, product_field_id=int(fid), value=field_values[j] if j<len(field_values) else ''))
            addon_ids=request.form.getlist(f'addon_id_{i}'); addon_qtys=request.form.getlist(f'addon_quantity_{i}')
            product_addons={a.id:a for a in Product.query.get(int(pid)).addons}
            for aid in addon_ids:
                if not aid or int(aid) not in product_addons: continue
                aid_int=int(aid); a=product_addons[aid_int]
                aq=money(request.form.get(f'addon_quantity_{i}_{aid_int}', '1'))
                db.session.add(SaleItemAddon(sale_item=item, product_addon_id=a.id, quantity=aq, unit_price=a.price))
                item.labor_hours = (item.labor_hours or Decimal('0')) + (a.labor_hours or Decimal('0')) * aq
                total += aq*a.price*q
            total += q*price
        sale.total_amount=total; db.session.commit(); flash('Sale updated.' if sale.id and request.path.endswith('/edit') else 'Sale recorded.','success'); return redirect(url_for('sales'))
    selected_items=[]
    if sale:
        for item in sale.items:
            selected_items.append({'product_id': item.product_id, 'bundle_id': item.bundle_id, 'quantity': str(item.quantity or 1), 'unit_price': str(item.unit_price or 0), 'labor_hours': str(item.labor_hours or 0), 'fields': {str(x.product_field_id): x.value for x in item.fields}, 'addons': {str(x.product_addon_id): str(x.quantity or 1) for x in item.addons}})
    return render_template('sales/form.html', sale=sale, products=products, platforms=Platform.query.filter_by(active=True).all(), payments=PaymentMethod.query.filter_by(active=True).all(), product_data=_product_json(products), selected_items=selected_items, now=(sale.sale_date if sale else datetime.utcnow()).strftime('%Y-%m-%dT%H:%M'))

@app.route('/sales/<int:id>/delete', methods=['POST'])
def sale_delete(id):
    db.session.delete(Sale.query.get_or_404(id)); db.session.commit(); flash('Sale removed.','success'); return redirect(url_for('sales'))

@app.route('/customers')
def customers():
    rows = db.session.query(Customer, func.count(Sale.id).label('purchase_count'),
        func.coalesce(func.sum(Sale.total_amount), 0).label('total_spent'),
        func.coalesce(func.avg(Sale.total_amount), 0).label('average_spent'),
        func.max(Sale.sale_date).label('last_purchase')
    ).outerjoin(Sale, Customer.id == Sale.customer_id).group_by(Customer.id).order_by(Customer.name).all()
    customer_stats = []
    for customer, purchase_count, total_spent, average_spent, last_purchase in rows:
        frequent = db.session.query(Product.name, func.coalesce(func.sum(SaleItem.quantity), 0).label('qty')).join(
            SaleItem, Product.id == SaleItem.product_id).join(Sale, Sale.id == SaleItem.sale_id).filter(
            Sale.customer_id == customer.id).group_by(Product.id).order_by(func.sum(SaleItem.quantity).desc()).first()
        customer_stats.append({'customer': customer, 'purchase_count': purchase_count, 'total_spent': total_spent,
            'average_spent': average_spent, 'last_purchase': last_purchase,
            'favorite_product': frequent[0] if frequent else None, 'favorite_quantity': frequent[1] if frequent else 0})
    top_spender = max(customer_stats, key=lambda x: x['total_spent'] or 0, default=None)
    most_frequent = max(customer_stats, key=lambda x: x['purchase_count'] or 0, default=None)
    return render_template('customers/list.html', customers=customer_stats, top_spender=top_spender, most_frequent=most_frequent)

@app.route('/customers/new', methods=['GET','POST'])
def customer_new(): return _customer_form(None)

@app.route('/customers/<int:id>/edit', methods=['GET','POST'])
def customer_edit(id): return _customer_form(Customer.query.get_or_404(id))

def _customer_form(customer):
    if request.method=='POST':
        if customer is None: customer=Customer(); db.session.add(customer)
        customer.name=request.form['name'].strip(); customer.email=request.form.get('email'); customer.username=request.form.get('username'); customer.notes=request.form.get('notes'); db.session.commit(); flash('Customer saved.','success'); return redirect(url_for('customers'))
    return render_template('customers/form.html', customer=customer)

@app.route('/customers/<int:id>/delete', methods=['POST'])
def customer_delete(id):
    c=Customer.query.get_or_404(id)
    if c.sales:
        flash('Customer cannot be deleted while sales are attached. Edit the customer or remove/assign those sales first.','error')
    else:
        db.session.delete(c); db.session.commit(); flash('Customer deleted.','success')
    return redirect(url_for('customers'))

@app.route('/analytics')
def analytics():
    products = db.session.query(Product.name, func.coalesce(func.sum(SaleItem.quantity),0).label('qty'),
        func.coalesce(func.sum(SaleItem.quantity*SaleItem.unit_price),0).label('revenue'),
        func.coalesce(func.sum(SaleItem.quantity*SaleItem.labor_hours),0).label('hours')).join(
        SaleItem, Product.id==SaleItem.product_id).group_by(Product.id).order_by(
        func.sum(SaleItem.quantity*SaleItem.unit_price).desc()).all()
    platform_rows = db.session.query(Platform.name, func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).join(
        Sale, Platform.id==Sale.platform_id).group_by(Platform.id).order_by(func.sum(Sale.total_amount).desc()).all()
    platforms = [{'name': r[0], 'revenue': float(r[1] or 0)} for r in platform_rows]
    payments = db.session.query(PaymentMethod.name, func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).join(
        Sale, PaymentMethod.id==Sale.payment_method_id).group_by(PaymentMethod.id).all()
    if db.engine.dialect.name == 'sqlite':
        monthly_rows = db.session.query(func.strftime('%Y-%m', Sale.sale_date).label('month'),
            func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).group_by(
            func.strftime('%Y-%m', Sale.sale_date)).order_by(func.strftime('%Y-%m', Sale.sale_date)).all()
    else:
        monthly_rows = db.session.execute(text("SELECT to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month, COALESCE(SUM(total_amount),0) AS revenue FROM sale GROUP BY date_trunc('month', sale_date) ORDER BY date_trunc('month', sale_date)")).fetchall()
    monthly_revenue = [{'month': r.month, 'revenue': float(r.revenue or 0)} for r in monthly_rows[-12:]]
    top_buyers = db.session.query(Customer.name, func.count(Sale.id).label('purchases'),
        func.coalesce(func.sum(Sale.total_amount),0).label('spent')).join(Sale, Customer.id==Sale.customer_id).group_by(
        Customer.id).order_by(func.sum(Sale.total_amount).desc()).limit(10).all()
    top_addons = db.session.query(ProductAddon.name, func.coalesce(func.sum(SaleItemAddon.quantity),0).label('qty'),
        func.coalesce(func.sum(SaleItemAddon.quantity*SaleItemAddon.unit_price),0).label('revenue')).join(
        SaleItemAddon, ProductAddon.id==SaleItemAddon.product_addon_id).group_by(ProductAddon.id).order_by(
        func.sum(SaleItemAddon.quantity*SaleItemAddon.unit_price).desc()).limit(10).all()
    return render_template('analytics/index.html', products=products, platforms=platforms, payments=payments,
        monthly_revenue=monthly_revenue, top_buyers=top_buyers, top_addons=top_addons)

@app.route('/settings', methods=['GET','POST'])
def settings():
    if request.method=='POST':
        model={'category':Category,'platform':Platform,'payment':PaymentMethod}[request.form['kind']]
        name=request.form['name'].strip()
        if name: db.session.add(model(name=name)); db.session.commit(); flash('Setting added.','success')
        return redirect(url_for('settings'))
    return render_template('settings/index.html', categories=Category.query.order_by(Category.name).all(), platforms=Platform.query.order_by(Platform.name).all(), payments=PaymentMethod.query.order_by(PaymentMethod.name).all())

@app.route('/settings/<kind>/<int:id>/edit', methods=['GET','POST'])
def setting_edit(kind,id):
    model={'category':Category,'platform':Platform,'payment':PaymentMethod}.get(kind)
    if not model: return redirect(url_for('settings'))
    obj=model.query.get_or_404(id)
    if request.method=='POST':
        obj.name=request.form['name'].strip();
        if hasattr(obj,'active'): obj.active=bool(request.form.get('active'))
        db.session.commit(); flash('Setting updated.','success'); return redirect(url_for('settings'))
    return render_template('settings/edit.html', kind=kind, title={'category':'Category','platform':'Platform','payment':'Payment Method'}[kind], obj=obj)

@app.route('/settings/<kind>/<int:id>/delete', methods=['POST'])
def setting_delete(kind,id):
    model={'category':Category,'platform':Platform,'payment':PaymentMethod}.get(kind)
    if not model: return redirect(url_for('settings'))
    obj=model.query.get_or_404(id)
    if kind=='category' and Product.query.filter_by(category_id=id).first(): flash('Category is in use by products.','error')
    elif kind=='platform' and Sale.query.filter_by(platform_id=id).first(): flash('Platform is in use by sales.','error')
    elif kind=='payment' and Sale.query.filter_by(payment_method_id=id).first(): flash('Payment method is in use by sales.','error')
    else: db.session.delete(obj); db.session.commit(); flash('Setting deleted.','success')
    return redirect(url_for('settings'))

if __name__ == '__main__': app.run(debug=True)
